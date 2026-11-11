"""Boot signature refusals: what persists, what the user is told, and post-pip checks."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from apps_backend_coverage_helpers import _capture_popen, _StopSpawn
from apps_backend_coverage_helpers import spawn_root_fixture as _spawn_root
from test_app_signed_provisioning import boot_app as _boot_app
from test_app_signed_round3 import SECRET, sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import (
    admission,
    backend,
    boot_refusal_notice,
    bridges,
    bridges_mcp,
    execution,
    manager,
)
from kiro_crew.apps import manifest as manifest_module
from kiro_crew.apps.backend_runtime import startup
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.signed_content import sign_app_dir

spawn_root = _spawn_root
signed_home = _signed_home
boot_app = _boot_app


@pytest.fixture(autouse=True)
def _no_held_notes():
    boot_refusal_notice.take_boot_refusal_notes()
    yield
    boot_refusal_notice.take_boot_refusal_notes()


def _make_refused(root: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Leave the installed tree at *root* in the state *kind* names."""
    if kind == "unsigned":
        write_app(root, backend={"entryPoint": "server.py"})
    elif kind == "untrusted_signer":
        sign_app_dir(root, signer="stranger", secret="stranger-secret")
    elif kind == "invalid_signature":
        # A trusted key's signature over the payload of an earlier scheme.
        with monkeypatch.context() as earlier:
            earlier.setattr(manifest_module, "APP_SIGNATURE_SCHEME", "kiro-crew-app/1")
            sign(root)
    else:
        (root / "server.py").write_text("VALUE = 'changed'\n", encoding="utf-8")


@pytest.mark.parametrize("phase", ["resources", "backend"])
@pytest.mark.parametrize(
    "kind", ["unsigned", "untrusted_signer", "invalid_signature", "content_mismatch"]
)
def test_boot_persists_only_a_trusted_content_mismatch_and_tells_the_user(
    boot_app, monkeypatch: pytest.MonkeyPatch, phase: str, kind: str
) -> None:
    root, snapshots, registered, revoked, _spawned = boot_app
    _make_refused(root, monkeypatch, kind)
    original = admission.app_admission_denied
    calls = []

    def admit(*args, **kwargs):
        calls.append(args)
        if phase == "backend" and len(calls) == 1:
            return None
        return original(*args, **kwargs)

    monkeypatch.setattr(startup, "app_admission_denied", admit)
    disable = Mock(wraps=manager.disable_app)
    monkeypatch.setattr(manager, "disable_app", disable)
    assert startup.start_enabled_app_backends() == []
    assert not snapshots[-1][0]["enabled"]
    assert revoked.call_count == 3
    persisted = kind == "content_mismatch"
    assert disable.call_count == (1 if persisted else 0)
    assert manager._read_installed("probe").enabled is (not persisted)

    state = Mock()
    assert boot_refusal_notice.deliver_boot_refusal_notes(state) == ["probe"]
    state.notify.assert_called_once()
    note_kind, title, body = state.notify.call_args.args
    assert note_kind == "update"
    assert title.startswith("probe ")
    assert ("turned off" in title) is persisted
    assert state.notify.call_args.kwargs["channel"] == "system.apps"
    assert state.notify.call_args.kwargs["url"] == "/apps/detail/probe"
    assert state.notify.call_args.kwargs["meta"] == {"app": "probe", "signatureRefusal": kind}
    next_step = {
        "unsigned": "Ask its publisher for a signed release",
        "untrusted_signer": "trust_keys",
        "invalid_signature": "Reinstall or update it from its source",
        "content_mismatch": "Reinstall or update it",
    }[kind]
    assert next_step in body
    # The note never tells the user to sign the tree it just refused.
    assert "kirocrew app sign" not in body
    # Delivered once: the next boot raises its own note.
    assert boot_refusal_notice.deliver_boot_refusal_notes(state) == []


def test_policy_refusal_raises_no_signature_note(
    boot_app, signed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = {
        "require_signature": True,
        "trust_keys": {"test-publisher": SECRET},
        "banned": ["probe"],
    }
    (signed_home / "app_admission.json").write_text(json.dumps(policy), encoding="utf-8")
    assert startup.start_enabled_app_backends() == []
    assert boot_refusal_notice.take_boot_refusal_notes() == []


def test_a_failing_note_does_not_stop_the_others(boot_app, monkeypatch: pytest.MonkeyPatch) -> None:
    boot_refusal_notice.record_boot_refusal("one", admission._SignatureRefusal("x"))
    boot_refusal_notice.record_boot_refusal("two", admission._SignatureRefusal("y"))
    boot_refusal_notice.record_boot_refusal("three", "a plain policy refusal")
    state = Mock()
    state.notify.side_effect = [RuntimeError("bell down"), None]
    assert boot_refusal_notice.deliver_boot_refusal_notes(state) == ["two"]


def test_held_notes_are_bounded() -> None:
    for index in range(boot_refusal_notice._MAX_PENDING + 5):
        boot_refusal_notice.record_boot_refusal(f"app{index}", admission._SignatureRefusal("x"))
    assert len(boot_refusal_notice.take_boot_refusal_notes()) == boot_refusal_notice._MAX_PENDING


def test_delivery_survives_an_unloadable_note_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "kiro_crew.apps.update_notify":
            raise ImportError("not loadable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    state = Mock()
    assert boot_refusal_notice.deliver_boot_refusal_notes(state) == []
    boot_refusal_notice.record_boot_refusal("one", admission._SignatureRefusal("x"))
    assert boot_refusal_notice.deliver_boot_refusal_notes(state) == []
    state.notify.assert_not_called()


@pytest.fixture
def python_app(spawn_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "policy"
    home.mkdir()
    monkeypatch.setattr(admission, "config_dir", lambda: home)
    (home / "app_admission.json").write_text(
        json.dumps({"require_signature": True, "trust_keys": {"test-publisher": SECRET}}),
        encoding="utf-8",
    )
    write_app(spawn_root, backend={"entryPoint": "server.py", "type": "python"})
    (spawn_root / "server.py").write_text("VALUE = 'published'\n", encoding="utf-8")
    (spawn_root / "requirements.txt").write_text("example-dist==1.0\n", encoding="utf-8")
    monkeypatch.setattr(admission, "_installed_root", lambda name: spawn_root)
    monkeypatch.setattr(
        manager,
        "get_app_manifest",
        lambda name: AppManifest.from_json_file(spawn_root / "app.json"),
    )
    monkeypatch.setattr(backend, "is_builtin_app", lambda **kwargs: False)
    monkeypatch.setattr(execution, "is_builtin_app", lambda **kwargs: False)
    monkeypatch.setattr(backend, "_pycache_prefix_env", lambda *args: {})
    sink = Mock()
    monkeypatch.setattr(backend, "sel", lambda: sink)
    monkeypatch.setattr(admission, "sel", lambda: sink)
    return spawn_root


@pytest.mark.parametrize("policy", [True, False])
@pytest.mark.parametrize("mutation", ["server", "manifest", None])
def test_backend_spawn_checks_signed_content_after_pip(
    python_app: Path, monkeypatch: pytest.MonkeyPatch, policy: bool, mutation: str | None
) -> None:
    held = sign(python_app)
    if not policy:
        (admission.config_dir() / "app_admission.json").unlink()

    def pip(app_name, root):
        if mutation == "server":
            (python_app / "server.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
        elif mutation == "manifest":
            document = json.loads((python_app / "app.json").read_text(encoding="utf-8"))
            document["description"] = "changed by a build hook"
            (python_app / "app.json").write_text(json.dumps(document), encoding="utf-8")
        return ""

    provision = Mock(side_effect=pip)
    monkeypatch.setattr(backend, "provision_app_deps", provision)
    spawned = _capture_popen(monkeypatch)
    if mutation and policy:
        assert backend._start_app_backend_body("probe", held) is None
        assert spawned == {}
        assert admission.sel().log_api_access.call_args.kwargs["outcome"] == "denied"
    else:
        with pytest.raises(_StopSpawn):
            backend._start_app_backend_body("probe", held)
        assert spawned["argv"]
    provision.assert_called_once()


@pytest.mark.parametrize("provision_result", ["", "pip failed"])
def test_a_pip_step_that_removes_requirements_cannot_skip_the_second_check(
    python_app: Path, monkeypatch: pytest.MonkeyPatch, provision_result: str
) -> None:
    """The check runs whatever the provisioner left behind, error return included."""
    held = sign(python_app)

    def pip(app_name, root):
        (python_app / "server.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
        (python_app / "requirements.txt").unlink()
        return provision_result

    monkeypatch.setattr(backend, "provision_app_deps", pip)
    spawned = _capture_popen(monkeypatch)
    assert backend._start_app_backend_body("probe", held) is None
    assert spawned == {}


def test_backend_spawn_refuses_when_the_post_pip_check_raises(
    python_app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = sign(python_app)
    monkeypatch.setattr(backend, "provision_app_deps", lambda *args: "")
    verify = Mock(side_effect=[None, OSError("verification unavailable")])
    monkeypatch.setattr(backend, "_installed_content_refusal", verify)
    spawned = _capture_popen(monkeypatch)
    assert backend._start_app_backend_body("probe", held) is None
    assert verify.call_count == 2
    assert spawned == {}


def _stdio_registration(signed_home: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    root = signed_home / "apps" / "probe"
    write_app(root, mcpServers={"test": {"command": "python", "args": ["server.py"]}})
    (root / "server.py").write_text("print('server')\n", encoding="utf-8")
    (root / "requirements.txt").write_text("example-dist==1.0\n", encoding="utf-8")
    saved: dict = {}
    monkeypatch.setattr(bridges, "app_dir", lambda n: signed_home / "apps" / n)
    monkeypatch.setattr(bridges, "shipped_builtin_app_root", lambda n: None)
    monkeypatch.setattr(bridges_mcp, "_live_port_for", lambda *args: None)
    monkeypatch.setattr(bridges, "_health_reconcile_guard", contextlib.nullcontext)
    monkeypatch.setattr(bridges, "_mcp_lock", contextlib.nullcontext)
    monkeypatch.setattr(bridges, "_read_mcp_json_unlocked", lambda **kwargs: {})
    monkeypatch.setattr(bridges, "_write_mcp_json_unlocked", saved.update)
    monkeypatch.setattr(bridges_mcp, "_schedule_unresolvable_warning", lambda *args: None)
    monkeypatch.setattr(execution, "is_builtin_app", lambda **kwargs: False)
    monkeypatch.setattr(admission, "sel", lambda: Mock())
    return root, saved


@pytest.mark.parametrize("policy", [True, False])
@pytest.mark.parametrize("mutation", [True, False])
def test_stdio_registration_checks_signed_content_after_pip(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, policy: bool, mutation: bool
) -> None:
    root, saved = _stdio_registration(signed_home, monkeypatch)
    manifest = sign(root)
    if not policy:
        (signed_home / "app_admission.json").unlink()

    def pip(app_name, app_root):
        if mutation:
            (root / "server.py").write_text("print('changed')\n", encoding="utf-8")
        return ""

    provision = Mock(side_effect=pip)
    monkeypatch.setattr(backend, "provision_app_deps", provision)
    deregistered = Mock(return_value=0)
    monkeypatch.setattr(bridges, "_deregister_mcp_servers", deregistered)
    registered = bridges_mcp._register_mcp_servers("probe", manifest)
    provision.assert_called_once()
    if mutation and policy:
        assert registered == []
        assert saved == {}
        deregistered.assert_called_once_with("probe")
    else:
        assert registered == ["probe:test"]
        assert "probe:test" in saved["mcpServers"]
        deregistered.assert_not_called()


def test_stdio_registration_refuses_when_the_post_pip_check_raises(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, saved = _stdio_registration(signed_home, monkeypatch)
    manifest = sign(root)
    monkeypatch.setattr(backend, "provision_app_deps", lambda *args: "")
    monkeypatch.setattr(
        bridges,
        "_installed_content_refusal",
        Mock(side_effect=OSError("verification unavailable")),
    )
    deregistered = Mock(return_value=0)
    monkeypatch.setattr(bridges, "_deregister_mcp_servers", deregistered)
    assert bridges_mcp._register_mcp_servers("probe", manifest) == []
    assert saved == {}
    deregistered.assert_called_once_with("probe")
