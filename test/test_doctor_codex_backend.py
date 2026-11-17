"""``kirocrew doctor`` reports the Codex harness's codex-acp adapter and its version.

Without this row a host whose selected backend is Codex and which has no codex-acp
printed no install line and passed doctor, while every Codex session failed to
spawn.

These tests call ``_doctor_codex_backend`` rather than ``_doctor()``, which shells out
and walks the host. They stub the adapter resolver, ``_resolve_codex_acp_bin``, which
both the install probe and the version reader go through, so the verdict comes from
the real ``probe_backend`` and the version from a real manifest under ``tmp_path``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_doctor
from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client_turn_control import CODEX_ACP_TESTED_VERSION, untested_version_issue
from kiro_crew.agent_sdk import UNKNOWN, BackendInstallState
from kiro_crew.agent_sdk import backend_install as probe
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.agent_sdk.drivers import acp as drivers_acp
from kiro_crew.doctor_checks import agents as doctor_agents

_ON_PATH = "/usr/local/bin/codex-acp"


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    """The probe caches verdicts in a module global, so clear it on both sides."""
    probe.clear_probe_cache()
    yield
    probe.clear_probe_cache()


def _install(tmp_path: Path, manifest: str | None) -> Path:
    """An npm layout of codex-acp under *tmp_path*, returning its entry script.

    *manifest* is the package.json text, or ``None`` for no package.json at all.
    """
    pkg = tmp_path / "node_modules" / acp_client.CODEX_ACP_NPM_PKG
    entry = pkg / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("process.stdin.resume()\n", encoding="utf-8")
    if manifest is not None:
        (pkg / "package.json").write_text(manifest, encoding="utf-8")
    return entry


def _manifest(version: str, name: str = acp_client.CODEX_ACP_NPM_PKG) -> str:
    return json.dumps({"name": name, "version": version})


def _report(
    monkeypatch,
    capsys,
    entry: Path | None,
    *,
    required: bool,
) -> tuple[str, list[str]]:
    """Run the row with the resolver answering *entry*, or "not found" for ``None``.

    ``which`` answers another copy on PATH, so a row that names it fails.
    """
    argv = (["node", str(entry)], "/usr/bin") if entry is not None else (None, "/usr/bin")
    monkeypatch.setattr(acp_client, "_resolve_codex_acp_bin", lambda: argv)
    monkeypatch.setattr(
        doctor_agents.shutil,
        "which",
        lambda name, **_kw: _ON_PATH if name == "codex-acp" else None,
    )
    issues: list[str] = []
    doctor_agents._doctor_codex_backend(issues, required=required)
    return capsys.readouterr().out, issues


def test_a_selected_codex_missing_its_adapter_is_an_issue(monkeypatch, capsys):
    """A selected role spawns codex-acp, so its absence fails every session."""
    out, issues = _report(monkeypatch, capsys, None, required=True)
    assert "codex-acp:   ❌ codex-acp not found (a selected backend runs it)" in out
    assert drivers_acp.codex_adapter_install_command() in out
    assert issues == ["codex-acp: codex-acp not found"]


def test_an_unselected_codex_missing_its_adapter_is_a_row_and_no_issue(monkeypatch, capsys):
    """Unselected, Codex is optional: the row still names the absence and the fix."""
    out, issues = _report(monkeypatch, capsys, None, required=False)
    assert "codex-acp:   ⏭  codex-acp not found (optional agent backend)" in out
    assert drivers_acp.codex_adapter_install_command() in out
    assert issues == []


def test_the_row_names_the_installed_version(tmp_path, monkeypatch, capsys):
    entry = _install(tmp_path, _manifest(CODEX_ACP_TESTED_VERSION))
    out, issues = _report(monkeypatch, capsys, entry, required=True)
    assert f"codex-acp:   ✅ {entry} {CODEX_ACP_TESTED_VERSION} (Codex installed)" in out
    assert "⚠️" not in out
    assert issues == []


@pytest.mark.parametrize("version", ["1.11.0", "3.0.0"])
def test_a_version_outside_the_tested_major_warns_and_is_no_issue(
    tmp_path, monkeypatch, capsys, version
):
    """Sessions still start on an untested release, so doctor warns and passes."""
    entry = _install(tmp_path, _manifest(version))
    out, issues = _report(monkeypatch, capsys, entry, required=True)
    notice = untested_version_issue(ACP_BACKEND_CODEX, version)
    assert notice
    assert f"codex-acp:   ✅ {entry} {version} (Codex installed)" in out
    assert f"⚠️  {notice}" in out
    assert issues == []


@pytest.mark.parametrize(
    "manifest",
    [
        "{not json",
        _manifest("1.11.0", name="@agentclientprotocol/claude-agent-acp"),
        json.dumps({"name": acp_client.CODEX_ACP_NPM_PKG}),
        None,
    ],
    ids=["not-json", "another-package", "no-version-field", "no-manifest"],
)
def test_an_unreadable_manifest_shows_no_version_and_is_no_issue(
    tmp_path, monkeypatch, capsys, manifest
):
    """An unread version is no evidence of an untested one."""
    entry = _install(tmp_path, manifest)
    out, issues = _report(monkeypatch, capsys, entry, required=True)
    assert f"codex-acp:   ✅ {entry} (Codex installed)" in out
    assert "1.11.0" not in out
    assert "⚠️" not in out
    assert issues == []


def test_the_version_is_read_without_running_the_adapter(tmp_path, monkeypatch):
    """codex-acp serves stdin on any argv, so running it would hang doctor."""
    entry = _install(tmp_path, _manifest(CODEX_ACP_TESTED_VERSION))
    monkeypatch.setattr(acp_client, "_resolve_codex_acp_bin", lambda: (["node", str(entry)], ""))

    def _no_spawn(*_a, **_k):
        raise AssertionError("the version must not be read by running the adapter")

    monkeypatch.setattr("subprocess.run", _no_spawn)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    assert drivers_acp.codex_adapter_disk_install() == (str(entry), CODEX_ACP_TESTED_VERSION)


def test_the_version_comes_from_the_entry_not_from_a_copy_beside_node(tmp_path, monkeypatch):
    """With ``[node, entry]``, node's own directory can hold another global copy."""
    node_dir = tmp_path / "node"
    beside_node = node_dir / "node_modules" / acp_client.CODEX_ACP_NPM_PKG
    beside_node.mkdir(parents=True)
    (beside_node / "package.json").write_text(_manifest("1.11.0"), encoding="utf-8")
    node = node_dir / "node.exe"
    node.write_text("", encoding="utf-8")
    entry = _install(tmp_path / "override", _manifest(CODEX_ACP_TESTED_VERSION))
    monkeypatch.setattr(acp_client, "_resolve_codex_acp_bin", lambda: ([str(node), str(entry)], ""))
    assert drivers_acp.codex_adapter_disk_install() == (str(entry), CODEX_ACP_TESTED_VERSION)


def test_the_row_names_the_adapter_the_spawn_resolves_not_the_copy_on_path(
    tmp_path, monkeypatch, capsys
):
    """CODEX_ACP_BIN, a vendored copy or a mise shim can win over the copy on PATH.

    The row then names the adapter that sessions run, with that adapter's version.
    """
    entry = _install(tmp_path, _manifest(CODEX_ACP_TESTED_VERSION))
    out, _issues = _report(monkeypatch, capsys, entry, required=False)
    assert f"codex-acp:   ✅ {entry} {CODEX_ACP_TESTED_VERSION} (Codex installed)" in out
    assert _ON_PATH not in out


@pytest.mark.parametrize("verdict", [UNKNOWN, "raises"])
def test_a_selected_codex_that_cannot_be_checked_is_no_issue(monkeypatch, capsys, verdict):
    """Only a MISSING verdict is evidence of absence."""

    def _probe(_backend):
        if verdict == "raises":
            raise RuntimeError("probe failed")
        return BackendInstallState(ACP_BACKEND_CODEX, "codex", verdict)

    monkeypatch.setattr("kiro_crew.agent_sdk.probe_backend", _probe)
    issues: list[str] = []
    doctor_agents._doctor_codex_backend(issues, required=True)
    out = capsys.readouterr().out
    assert "codex-acp:   ⚠️  could not check" in out
    assert "not found" not in out
    assert issues == []


def _cfg(main: str, member: str) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=main, member_acp_backend=member))


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        (_cfg(ACP_BACKEND_CODEX, ACP_BACKEND_KIRO), True),
        (_cfg(ACP_BACKEND_KIRO, ACP_BACKEND_CODEX), True),
        (_cfg(ACP_BACKEND_CODEX, ACP_BACKEND_CODEX), True),
        (_cfg(ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO), False),
        (_cfg(ACP_BACKEND_KAS, ACP_BACKEND_KIRO), False),
    ],
)
def test_either_selected_role_makes_codex_required(cfg, expected):
    assert cli_doctor._selected_roles_run_codex(cfg) is expected


def test_doctor_passes_the_selection_to_the_row():
    """Read from source because running ``_doctor`` reaches the host."""
    src = inspect.getsource(cli_doctor._doctor)
    assert "_doctor_codex_backend(issues, required=_selected_roles_run_codex(cfg))" in src
