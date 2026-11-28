"""Current release identity, immutable policy and fail-closed packaging regression."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script(filename: str):
    spec = importlib.util.spec_from_file_location(f"_audit_current_{filename}", ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_public_product_versions_match_without_mutating_policy_pack():
    from kiro_byo import __version__
    from kiro_byo.protocol import AGENT_VERSION
    # Source of truth is the VERSION file; every surface must agree with it.
    # Do NOT hard-code the version string here, or this test rots on every bump.
    version = (ROOT / "VERSION").read_text().strip()
    assert __version__ == AGENT_VERSION == version
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == version
    assert json.loads((ROOT / "app.json").read_text())["version"] == version
    # Immutability guard: the dyna policy pack must stay byte-stable. The
    # expected hash lives in a version-controlled manifest (policy_pack_hashes.json)
    # next to the packs, NOT as a magic constant in this test -- so a deliberate
    # pack change is a reviewable one-line manifest update, and an ACCIDENTAL
    # change still fails the guard. This stops the per-release "copy the audit
    # test and paste a new hash" rot.
    dyna = (ROOT / "src/kiro_byo/policy_packs/dyna.json").read_bytes()
    manifest = json.loads((ROOT / "src/kiro_byo/policy_packs/policy_pack_hashes.json").read_text())
    assert hashlib.sha256(dyna).hexdigest() == manifest["dyna.json"]


def test_release_archive_does_not_include_credentials_or_mutable_state(tmp_path):
    package = _load_script("build_release.py")
    files = package._relative_files()
    names = {p.as_posix() for p in files}
    assert "config/providers.example.json" in names
    assert "src/kiro_byo/crew_ledger_bridge.py" in names
    assert "config/providers.json" not in names
    assert all("data/runs/" not in name and "data/sessions/" not in name for name in names)
    assert all(not name.endswith((".pem", ".key", ".pfx", ".p12")) for name in names)
    assert "version = _version()" in (ROOT / "scripts/build_release.py").read_text()
    assert "not_wired_into_official_Crew_dispatch" in (ROOT / "scripts/build_release.py").read_text()


def test_sensitive_untracked_files_fail_release_gate(tmp_path, monkeypatch):
    gate = _load_script("release_check.py")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    for file in (".env.local", "secret.pem", "private_access.key", "credentials.json"):
        (tmp_path / file).write_text("synthetic", encoding="utf-8")
    assert "sensitive artifacts" in gate._source_hygiene_release_check()
    for file in (".env.local", "secret.pem", "private_access.key", "credentials.json"):
        (tmp_path / file).unlink()
    assert gate._source_hygiene_release_check() is None


def test_crew_task_link_is_diagnostic_not_authorization():
    from kiro_byo.crew_ledger_bridge import add_crew_work_link_to_prompt, byo_work_correlation_advertised
    from kiro_byo.protocol import initialize_result
    assert byo_work_correlation_advertised(initialize_result())
    plain = {"sessionId": "s", "prompt": [{"type": "text", "text": "hello"}]}
    link = {"taskId": "crew-work:" + "a" * 32, "role": "worker"}
    assert add_crew_work_link_to_prompt(plain, backend="kas", extension_advertised=False, work_link=link) == plain
    assert "_meta" not in plain
    linked = add_crew_work_link_to_prompt(plain, backend="kiro-byo", extension_advertised=True, work_link=link)
    assert linked["_meta"]["kiro"]["byoWorkLink"] == link
    assert "permissions" not in linked and "toolApproval" not in linked
