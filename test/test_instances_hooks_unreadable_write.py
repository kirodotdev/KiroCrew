"""A failed read of ``instances.json`` or ``hooks.json`` is never written back.

Both stores degrade a present file they cannot read to an empty one, so no reader
fails. A mutation that started from that empty answer replaced every record in the
file with its one change. The registry's mutators now read strictly and raise
``RegistryUnreadableError`` (503 at the instances routes), leaving the file as it
is; a UTF-8 byte-order mark reads normally. A hook store whose load failed reloads
before its next mutation and refuses with ``WebhookStoreUnreadable`` while the
file still cannot be loaded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from instances_helpers import _body, _enable, _FakeReq, _State

from kiro_crew import webhooks
from kiro_crew.cloud import connect
from kiro_crew.dashboard import handlers_instances as handlers
from kiro_crew.hooks import ScriptHookStore
from kiro_crew.instances import registry as registry_mod
from kiro_crew.instances.registry import InstancesRegistry

BOM = b"\xef\xbb\xbf"
_SHARING_VIOLATION = (13, "The process cannot access the file because it is being used")
# Bound before any test patches ``Path.read_bytes``, so assertions read the real bytes.
_REAL_READ_BYTES = Path.read_bytes


def _bytes_of(path: Path) -> bytes:
    return _REAL_READ_BYTES(path)


def _seed_two(path: Path) -> InstancesRegistry:
    reg = InstancesRegistry(path=path)
    reg.add(name="alpha", ssh_host="alpha.example.invalid")
    reg.add(name="beta", ssh_host="beta.example.invalid")
    return reg


def _names(path: Path) -> list[str]:
    return [i["name"] for i in json.loads(path.read_text(encoding="utf-8-sig"))["instances"]]


def _fail_reads_of(path: Path, monkeypatch, *, readers: bool = True, times: int = -1) -> None:
    """Every read of *path* raises the error a Windows share-mode-0 handle gives.

    ``readers=False`` fails only the retried read a registry mutation makes, so
    the registry's readers still see the file. ``times`` bounds how many reads
    fail before the handle is gone.
    """
    left = [times]

    def _held(original):
        def read(self, *args, **kwargs):
            if Path(self) == path and left[0] != 0:
                left[0] -= 1
                raise PermissionError(*_SHARING_VIOLATION)
            return original(self, *args, **kwargs)

        return read

    if readers:
        monkeypatch.setattr(Path, "read_bytes", _held(Path.read_bytes))
    else:
        monkeypatch.setattr(
            registry_mod, "read_bytes_with_retry", _held(registry_mod.read_bytes_with_retry)
        )


# ── instances.json ───────────────────────────────────────────────────────


def test_an_add_over_a_bom_registry_keeps_every_instance(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    reg = _seed_two(path)
    path.write_bytes(BOM + path.read_bytes())
    reg.add(name="gamma", ssh_host="gamma.example.invalid")
    assert _names(path) == ["alpha", "beta", "gamma"]


def test_readers_see_a_bom_registrys_instances(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    reg = _seed_two(path)
    path.write_bytes(BOM + path.read_bytes())
    assert [i.name for i in reg.list()] == ["alpha", "beta"]


def _mutations(reg: InstancesRegistry) -> dict:
    alpha = reg.list()[0].id
    return {
        "add": lambda: reg.add(name="gamma", ssh_host="gamma.example.invalid"),
        "update": lambda: reg.update(alpha, name="renamed"),
        "lend_hop": lambda: reg.lend_hop(41000, 1.0),
        "remove_cascade": lambda: reg.remove_cascade(alpha),
        "remove": lambda: reg.remove(alpha),
    }


@pytest.mark.parametrize("mutation", ["add", "update", "lend_hop", "remove_cascade", "remove"])
def test_a_failed_read_refuses_every_mutation_and_keeps_the_file(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    path = tmp_path / "instances.json"
    reg = _seed_two(path)
    run = _mutations(reg)[mutation]
    original = path.read_bytes()
    _fail_reads_of(path, monkeypatch)
    with pytest.raises(registry_mod.RegistryUnreadableError, match="instances.json"):
        run()
    assert _bytes_of(path) == original


@pytest.mark.parametrize(
    "content",
    [
        b'{"instances": [',
        b"[]",
        b'{"instances": {"alpha": {}}}',
        b'{"instances": [{"id": "caf\xe9"}]}',
    ],
    ids=["truncated", "list-root", "instances-mapping", "undecodable"],
)
def test_an_unparseable_registry_refuses_the_add_and_keeps_the_file(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "instances.json"
    path.write_bytes(content)
    reg = InstancesRegistry(path=path)
    with pytest.raises(registry_mod.RegistryUnreadableError, match="instances.json"):
        reg.add(name="gamma", ssh_host="gamma.example.invalid")
    assert path.read_bytes() == content


@pytest.mark.parametrize(
    "content",
    [b'{"instances": [', b"[]", b'{"instances": {"a": {}}}'],
    ids=["truncated", "list-root", "instances-mapping"],
)
def test_readers_still_degrade_an_unparseable_registry_to_empty(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "instances.json"
    path.write_bytes(content)
    assert InstancesRegistry(path=path).list() == []


def test_a_missing_registry_still_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    InstancesRegistry(path=path).add(name="gamma", ssh_host="gamma.example.invalid")
    assert _names(path) == ["gamma"]


def test_cloud_registration_reports_failure_and_keeps_the_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "instances.json"
    _seed_two(path)
    original = path.read_bytes()
    monkeypatch.setattr(registry_mod, "_DEFAULT_DIR", tmp_path)
    _fail_reads_of(path, monkeypatch)
    registered = connect.register_instance(
        "i-0123456789abcdef0", name="gamma", profile="default", region="us-east-1"
    )
    assert registered is None
    assert _bytes_of(path) == original


class TestRoutesAnswer503:
    def _state(self, tmp_path: Path, monkeypatch):
        _enable(tmp_path, monkeypatch)
        path = tmp_path / "instances.json"
        reg = _seed_two(path)
        return _State(reg), reg, path

    def _assert_refused(self, r, path: Path, original: bytes) -> None:
        assert r.status == 503
        body = _body(r)
        assert body["code"] == "instances_file_unreadable"
        assert "instances.json" in body["error"]
        assert _bytes_of(path) == original

    def test_add(self, tmp_path: Path, monkeypatch) -> None:
        state, _, path = self._state(tmp_path, monkeypatch)
        original = path.read_bytes()
        _fail_reads_of(path, monkeypatch, readers=False)
        req = _FakeReq(state, body={"name": "gamma", "ssh_host": "gamma.example.invalid"})
        self._assert_refused(asyncio.run(handlers.api_instances_add(req)), path, original)

    def test_update(self, tmp_path: Path, monkeypatch) -> None:
        state, reg, path = self._state(tmp_path, monkeypatch)
        alpha = reg.list()[0].id
        original = path.read_bytes()
        _fail_reads_of(path, monkeypatch, readers=False)
        req = _FakeReq(state, match={"id": alpha}, body={"name": "renamed"})
        self._assert_refused(asyncio.run(handlers.api_instances_update(req)), path, original)

    def test_remove(self, tmp_path: Path, monkeypatch) -> None:
        state, reg, path = self._state(tmp_path, monkeypatch)
        alpha = reg.list()[0].id
        original = path.read_bytes()
        _fail_reads_of(path, monkeypatch, readers=False)
        req = _FakeReq(state, match={"id": alpha})
        self._assert_refused(asyncio.run(handlers.api_instances_remove(req)), path, original)


# ── hooks.json ───────────────────────────────────────────────────────────


def _hook(hook_id: str) -> dict:
    return {"id": hook_id, "event": "UserPromptSubmit", "command": f"echo {hook_id}"}


def _hook_ids(hooks_dir: Path) -> list[str]:
    return [h["id"] for h in json.loads((hooks_dir / "hooks.json").read_text("utf-8"))["hooks"]]


def _seed_hooks(hooks_dir: Path) -> Path:
    store = ScriptHookStore(config_dir=hooks_dir)
    for hook_id in ("h1", "h2"):
        store.create(_hook(hook_id))
    return hooks_dir / "hooks.json"


def test_a_store_whose_load_failed_reloads_before_its_first_edit(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_hooks(tmp_path)
    with pytest.MonkeyPatch.context() as patched:
        _fail_reads_of(tmp_path / "hooks.json", patched)
        store = ScriptHookStore(config_dir=tmp_path)
    assert store.list_all() == []
    store.create(_hook("h3"))
    assert _hook_ids(tmp_path) == ["h1", "h2", "h3"]
    assert [h.id for h in store.list_all()] == ["h1", "h2", "h3"]


@pytest.mark.parametrize("mutation", ["create", "update", "toggle", "delete"])
def test_a_store_that_still_cannot_load_refuses_every_edit_and_keeps_the_file(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    path = _seed_hooks(tmp_path)
    original = path.read_bytes()
    _fail_reads_of(path, monkeypatch)
    store = ScriptHookStore(config_dir=tmp_path)
    run = {
        "create": lambda: store.create(_hook("h3")),
        "update": lambda: store.update("h1", {"command": "echo changed"}),
        "toggle": lambda: store.toggle("h1"),
        "delete": lambda: store.delete("h1"),
    }[mutation]
    with pytest.raises(webhooks.WebhookStoreUnreadable, match="hooks.json"):
        run()
    assert _bytes_of(path) == original


def test_status_bookkeeping_never_saves_a_failed_load_over_the_file(
    tmp_path: Path, monkeypatch
) -> None:
    path = _seed_hooks(tmp_path)
    original = path.read_bytes()
    with pytest.MonkeyPatch.context() as patched:
        _fail_reads_of(path, patched)
        store = ScriptHookStore(config_dir=tmp_path)
    store._persist_current()
    assert _bytes_of(path) == original


def test_a_reload_that_raises_keeps_the_store_marked_unloaded(tmp_path: Path) -> None:
    path = _seed_hooks(tmp_path)
    repaired = path.read_bytes()
    path.write_text("[]", encoding="utf-8")
    store = ScriptHookStore(config_dir=tmp_path)
    real_exists = Path.exists

    def exists(self, *args, **kwargs):
        if Path(self) == path:
            raise PermissionError(*_SHARING_VIOLATION)
        return real_exists(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(Path, "exists", exists)
        with pytest.raises(PermissionError):
            store.create(_hook("h3"))
    path.write_bytes(repaired)
    store.create(_hook("h3"))
    assert _hook_ids(tmp_path) == ["h1", "h2", "h3"]


def test_a_store_built_over_a_bad_root_picks_up_the_repaired_file(tmp_path: Path) -> None:
    path = _seed_hooks(tmp_path)
    repaired = path.read_bytes()
    path.write_text("[]", encoding="utf-8")
    store = ScriptHookStore(config_dir=tmp_path)
    path.write_bytes(repaired)
    store.create(_hook("h3"))
    assert _hook_ids(tmp_path) == ["h1", "h2", "h3"]


def test_a_missing_hooks_file_still_starts_empty(tmp_path: Path) -> None:
    store = ScriptHookStore(config_dir=tmp_path)
    store.create(_hook("h1"))
    assert _hook_ids(tmp_path) == ["h1"]
