"""Declarative registrations of a signed self-registered app check its signature only.

``register_external_app`` keeps only metadata in the installed directory, so the
crons and MCP config it declares are registered from a tree that holds none of
the signed files. A registration that runs and reads nothing from the tree (message
or agent crons, URL-only MCP servers) verifies the signature, using the rule
activation uses (``admission._activation_tree``). A packaged file in that
directory, a cron script or command, a stdio MCP server, agent and skill
registration, and code the gateway would execute from it are still compared with
``fileDigests``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_app_signed_external_register import _register, _signed_external
from test_app_signed_round3 import signed_home as _signed_home

from kiro_crew.apps import admission, manager

signed_home = _signed_home


def _manifest():
    manifest = manager.get_app_manifest("probe")
    assert manifest is not None
    return manifest


def test_declarative_registration_of_signed_external_app_is_admitted(
    signed_home: Path, tmp_path: Path
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    manifest = _manifest()
    refusal, verified = admission.verified_installed_manifest(
        "probe", metadata, manifest, declarative=True
    )
    assert refusal is None
    assert verified is manifest
    assert (
        admission.installed_content_refusal("probe", metadata, manifest, declarative=True) is None
    )


def test_declarative_registration_still_needs_a_valid_signature(
    signed_home: Path, tmp_path: Path
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    manifest = _manifest()
    manifest.signature = "0" * len(manifest.signature)
    refusal = admission.installed_content_refusal("probe", metadata, manifest, declarative=True)
    assert refusal is not None and "signature" in refusal


def test_packaged_file_planted_in_metadata_directory_is_still_refused(
    signed_home: Path, tmp_path: Path
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    (metadata / "planted.py").write_text("print('unsigned')\n", encoding="utf-8")
    refusal = admission.installed_content_refusal("probe", metadata, _manifest(), declarative=True)
    assert refusal is not None and "do not match its signature" in refusal


def test_code_executed_from_metadata_directory_is_still_refused(
    signed_home: Path, tmp_path: Path
) -> None:
    metadata = _register(tmp_path, _signed_external(tmp_path))
    manifest = _manifest()
    # A file backend or hook names a file; declarative never covers it.
    executed = metadata / "server.py"
    refusal = admission.installed_content_refusal("probe", executed, manifest, declarative=True)
    assert refusal is not None and "server.py" in refusal
    # A lifecycle script runs in the tree: declarative never covers it either.
    refusal = admission.installed_content_refusal(
        "probe", metadata, manifest, "echo hi", declarative=True
    )
    assert refusal is not None
    # Without the declarative flag the directory is judged as executed code.
    refusal = admission.installed_content_refusal("probe", metadata, manifest)
    assert refusal is not None and "server.py" in refusal


@pytest.mark.parametrize("declarative", [True, False])
def test_without_policy_nothing_is_checked(
    signed_home: Path, tmp_path: Path, declarative: bool
) -> None:
    (signed_home / "app_admission.json").unlink()
    metadata = _register(tmp_path, _signed_external(tmp_path))
    assert admission.verified_installed_manifest(
        "probe", metadata, _manifest(), declarative=declarative
    ) == (None, None)


# ---------------------------------------------------------------------------
# Callers take the declarative path only for registrations that execute nothing
# from the installed directory.
# ---------------------------------------------------------------------------


def _signed_external_with(tmp_path: Path, **extra: object) -> dict:
    """A signed external tree declaring *extra* manifest fields."""
    import json

    from test_app_signed_round3 import sign, write_app

    tree = tmp_path / "external-app"
    write_app(tree, **extra)
    (tree / "server.py").write_text("print('runs outside the gateway')\n", encoding="utf-8")
    sign(tree)
    return json.loads((tree / "app.json").read_text(encoding="utf-8"))


def _gate_on_content_only(monkeypatch: pytest.MonkeyPatch, metadata: Path) -> None:
    """Run the real content check from the registration source, nothing else."""
    import kiro_crew.apps.bridges as brmod
    from kiro_crew.apps import bridges_sources

    monkeypatch.setattr(bridges_sources, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(brmod, "_activation_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(brmod, "_registration_source", lambda name: (_manifest(), metadata))


def _crons_app(tmp_path: Path, cron: dict) -> Path:
    document = _signed_external_with(
        tmp_path, permissions={"cron": True}, crons=[{"name": "tick", "every": 600, **cron}]
    )
    return _register(tmp_path, document)


def test_message_only_crons_of_signed_external_app_are_admitted(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges_crons

    metadata = _crons_app(tmp_path, {"agent": "probe-agent", "message": "hello"})
    _gate_on_content_only(monkeypatch, metadata)
    defs, _ = bridges_crons._admitted_app_cron_defs("probe")
    assert [d["message"] for d in defs] == ["hello"]
    assert bridges_crons._cron_restore_denied("probe") is False


@pytest.mark.parametrize(
    "cron",
    [{"script": "data/tick.py:run"}, {"command": "python3 data/tick.py"}],
    ids=["script", "command"],
)
def test_executing_cron_of_signed_external_app_keeps_full_comparison(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cron: dict
) -> None:
    from kiro_crew.apps import bridges_crons

    metadata = _crons_app(tmp_path, cron)
    # Unsigned bytes in the inventory-excluded root ``data``: the directory still
    # holds no packaged file, so only the caller can tell that this job runs code.
    (metadata / "data").mkdir(exist_ok=True)
    (metadata / "data" / "tick.py").write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    _gate_on_content_only(monkeypatch, metadata)
    defs, _ = bridges_crons._admitted_app_cron_defs("probe")
    assert defs == []
    assert bridges_crons._cron_restore_denied("probe") is True


def _mcp_app(tmp_path: Path, server: dict, **extra: object) -> Path:
    document = _signed_external_with(tmp_path, mcpServers={"tools": server}, **extra)
    return _register(tmp_path, document)


def _capture_mcp(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    import kiro_crew.apps.bridges as brmod

    calls = {"registered": 0, "deregistered": 0}

    def _register_servers(*_a, **_k):
        calls["registered"] += 1
        return ["probe:tools"]

    def _deregister(*_a, **_k):
        calls["deregistered"] += 1
        return 0

    monkeypatch.setattr(brmod, "_register_mcp_servers", _register_servers)
    monkeypatch.setattr(brmod, "_deregister_mcp_servers", _deregister)
    monkeypatch.setattr(brmod, "_register_agents", lambda *a, **k: None)
    return calls


def test_url_only_mcp_server_of_signed_external_app_is_admitted(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges_mcp

    metadata = _mcp_app(tmp_path, {"url": "http://127.0.0.1:9100/mcp"})
    _gate_on_content_only(monkeypatch, metadata)
    calls = _capture_mcp(monkeypatch)
    assert bridges_mcp.reregister_app_mcp_servers("probe") == ["probe:tools"]
    assert bridges_mcp.scrub_backend_mcp_url("probe") == ["probe:tools"]
    assert calls == {"registered": 2, "deregistered": 0}


@pytest.mark.parametrize(
    "server",
    [
        {"command": "python3", "args": ["server.py"]},
        {"url": "http://127.0.0.1:9100/mcp", "command": "python3"},
    ],
    ids=["stdio", "url-with-command"],
)
def test_command_mcp_server_of_signed_external_app_keeps_full_comparison(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: dict
) -> None:
    from kiro_crew.apps import bridges_mcp

    metadata = _mcp_app(tmp_path, server)
    _gate_on_content_only(monkeypatch, metadata)
    calls = _capture_mcp(monkeypatch)
    assert bridges_mcp.reregister_app_mcp_servers("probe") == []
    assert bridges_mcp.scrub_backend_mcp_url("probe") == []
    assert calls == {"registered": 0, "deregistered": 2}


def test_mcp_reregistration_that_materializes_agents_keeps_full_comparison(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges_mcp

    metadata = _mcp_app(
        tmp_path,
        {"url": "http://127.0.0.1:9100/mcp"},
        agents=[{"name": "helper", "description": "d", "prompt": "p"}],
    )
    _gate_on_content_only(monkeypatch, metadata)
    calls = _capture_mcp(monkeypatch)
    assert _manifest().agents
    assert bridges_mcp.reregister_app_mcp_servers("probe") == []
    assert calls == {"registered": 0, "deregistered": 1}


def test_agent_and_skill_registration_keep_full_comparison(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.apps.bridges as brmod
    from kiro_crew.apps import bridges_sources

    metadata = _register(tmp_path, _signed_external(tmp_path))
    monkeypatch.setattr(bridges_sources, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(brmod, "_activation_admission_denied", lambda *a, **k: None)
    # The default the agent and skill bridges use: the metadata directory is
    # compared with the signed list, which it does not hold.
    denied = bridges_sources._registration_denied(
        "probe", action="resource_register", app_root=metadata, manifest=_manifest()
    )
    assert denied
    assert (
        bridges_sources._registration_denied(
            "probe",
            action="resource_register",
            app_root=metadata,
            manifest=_manifest(),
            declarative=True,
        )
        is None
    )


def test_without_policy_executing_registrations_are_unchanged(
    signed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import bridges_crons, bridges_sources

    (signed_home / "app_admission.json").unlink()
    metadata = _crons_app(tmp_path, {"script": "data/tick.py:run"})
    (metadata / "data").mkdir(exist_ok=True)
    (metadata / "data" / "tick.py").write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    _gate_on_content_only(monkeypatch, metadata)
    assert bridges_crons._cron_restore_denied("probe") is False
    assert (
        bridges_sources._registration_denied(
            "probe", action="mcp_register", app_root=metadata, manifest=_manifest()
        )
        is None
    )
