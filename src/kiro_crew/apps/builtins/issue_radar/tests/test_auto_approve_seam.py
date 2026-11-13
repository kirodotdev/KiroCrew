"""Issue Radar holds its unattended crews' grant through the App Kit seam, and only it.

The crews' auto-approve grant once was this app's own call into the core's private
``SafetyOverride``. It now goes through ``kiro_crew.apps.worker_auto_approve``, which an
installed copy reaches as ``ctx.auto_approve``. Two things keep that true:

- the manifest declares ``permissions.workerAutoApprove``, which is what the seam reads
  on every grant, so the built-in and an adopted copy are judged by the same row;
- no module of the backend imports ``kiro_crew.safety_override``, so nothing reaches
  the grant store around the seam.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from kiro_crew.apps import worker_auto_approve
from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr

_ROOT = Path(__file__).resolve().parents[1]


def test_the_manifest_declares_the_grant():
    manifest = json.loads((_ROOT / "app.json").read_text(encoding="utf-8"))
    assert manifest["permissions"].get("workerAutoApprove") is True


def test_no_backend_module_reaches_the_grant_store_around_the_seam():
    offenders = []
    for path in sorted((_ROOT / "backend").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if any(n.startswith("kiro_crew.safety_override") for n in names):
                offenders.append(f"{path.relative_to(_ROOT)}:{getattr(node, 'lineno', 0)}")
    assert not offenders, offenders


def test_the_crew_scope_is_the_seams_key_in_this_apps_namespace():
    assert cr.autoapprove_scope("c_1") == worker_auto_approve.grant_scope(cr.APP_NAME, "crew-c_1")
    assert cr.GRANT_SOURCE == f"app:{cr.APP_NAME}"


def test_the_registered_builtin_holds_the_grant_through_the_real_gate(
    tmp_path, monkeypatch, sel_private_root
):
    """The packaged built-in, registered as at startup, passes the seam's own gate.

    No stub of ``app_holds_worker_auto_approve``: the built-in's grant rests on
    registration storing no approved-grants file and on ``staged_app_grants``
    reading a missing file as "the packaged declaration is approved". If either
    changed, unattended crews would silently fall back to approval prompts.
    """
    from types import SimpleNamespace

    from kiro_crew import safety_override as so_mod
    from kiro_crew.apps import manager
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.platform import context as ctx_mod

    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    ctx_mod.reset_context()
    so_mod.reset_yolo_policy_state()
    so_mod.reset_singleton()
    monkeypatch.setattr(worker_auto_approve, "_held", {})
    try:
        manager.register_builtin_apps()
        assert manager.enable_app(cr.APP_NAME).ok
        manager.register_builtin_apps()
        assert worker_auto_approve.app_holds_worker_auto_approve(cr.APP_NAME)

        slot = SimpleNamespace(key="w", _app=cr.APP_NAME, _trust=False, _trust_scope="")
        name = "crew-x"
        assert worker_auto_approve.WorkerAutoApprove(cr.APP_NAME).hold(
            slot, name, ttl_secs=900  # flake-ok: a grant lifetime, never waited on
        )
        assert slot._trust_scope == worker_auto_approve.grant_scope(cr.APP_NAME, name)
        assert chat_runner._slot_is_trusted(slot)
    finally:
        worker_auto_approve.revoke_app(cr.APP_NAME)
        so_mod.reset_yolo_policy_state()
        so_mod.reset_singleton()
        ctx_mod.reset_context()
