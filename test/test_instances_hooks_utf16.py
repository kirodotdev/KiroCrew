"""A UTF-16 ``instances.json`` or ``hooks.json`` (Windows PowerShell 5.1's default) loads.

Bytes no Unicode encoding decodes degrade the readers with a warning and refuse every write.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import kiro_crew.hooks as hooks_mod
from kiro_crew import webhooks
from kiro_crew.config import loader
from kiro_crew.hooks import ScriptHookStore
from kiro_crew.instances.registry import InstancesRegistry, RegistryUnreadableError
from kiro_crew.portability_vetting import _disable_imported_script_hooks

# A cp1252 "é": a legacy code-page byte that json.loads cannot decode as UTF-8.
_LEGACY_ID = b'"caf\xe9"'


def _utf16(obj: object) -> bytes:
    """What PowerShell 5.1 ``Out-File`` writes: a byte-order mark, UTF-16, CRLF."""
    return json.dumps(obj, indent=2).replace("\n", "\r\n").encode("utf-16")


def _hook(hook_id: str) -> dict:
    return {"id": hook_id, "event": "UserPromptSubmit", "command": f"echo {hook_id}"}


def _saved(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


def test_a_utf16_registry_lists_and_keeps_its_instances(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    alpha = {"id": "alpha", "name": "alpha", "ssh_host": "alpha.example.invalid"}
    path.write_bytes(_utf16({"instances": [alpha]}))
    reg = InstancesRegistry(path=path)
    assert [i.id for i in reg.list()] == ["alpha"]
    reg.add(name="beta", ssh_host="beta.example.invalid")
    assert [i["id"] for i in _saved(path)["instances"]] == ["alpha", "beta"]


def test_a_utf16_hooks_file_loads_and_survives_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    path = tmp_path / "hooks.json"
    path.write_bytes(_utf16({"r1": {"context_summary": "a"}, "hooks": [_hook("h1")]}))
    store = ScriptHookStore(config_dir=tmp_path)
    assert [h.id for h in store.list_all()] == ["h1"]
    assert [h.id for h in hooks_mod.persisted_hook_store().list_all()] == ["h1"]
    store.create(_hook("h2"))
    saved = _saved(path)
    assert [h["id"] for h in saved["hooks"]] == ["h1", "h2"]
    assert saved["r1"] == {"context_summary": "a"}


def test_an_imported_utf16_hooks_file_has_its_command_hooks_switched_off(tmp_path: Path) -> None:
    # The import sanitizer must read every file the store loads: a UTF-16 bundle
    # file it skipped would arrive with its command hooks still switched on.
    path = tmp_path / "hooks.json"
    path.write_bytes(_utf16({"hooks": [_hook("h1")]}))
    assert _disable_imported_script_hooks(path) == ["h1"]
    assert [h.enabled for h in ScriptHookStore(config_dir=tmp_path).list_all()] == [False]


def test_an_undecodable_registry_reads_as_empty_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "instances.json"
    content = b'{"instances": [{"id": ' + _LEGACY_ID + b"}]}"
    path.write_bytes(content)
    reg = InstancesRegistry(path=path)
    with caplog.at_level(logging.WARNING):
        assert reg.list() == []
    assert "treating as empty" in caplog.text
    with pytest.raises(RegistryUnreadableError):
        reg.add(name="beta", ssh_host="beta.example.invalid")
    assert path.read_bytes() == content


def test_an_undecodable_hooks_file_loads_empty_and_refuses_every_edit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "hooks.json"
    content = b'{"hooks": [{"id": ' + _LEGACY_ID + b"}]}"
    path.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        store = ScriptHookStore(config_dir=tmp_path)
    assert store.list_all() == []
    assert "Failed to load hooks" in caplog.text
    with pytest.raises(webhooks.WebhookStoreUnreadable):
        store.create(_hook("h2"))
    assert path.read_bytes() == content
