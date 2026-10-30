"""Durable ownership registry for per-parent-session Coder workspaces (under ``tmp_path``)."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.coder.registry as registry_mod
from kiro_crew import platform_compat
from kiro_crew.coder.registry import (
    WorkspaceBinding,
    WorkspaceBindingRegistry,
    WorkspaceRegistryCorrupt,
)
from kiro_crew.constants import CODER_WORKSPACE_PREFIX_MAX_CHARS

_CODER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


_ALLOCATE_DEFAULTS = dict(
    template="kirocrew-arm",
    preset="",
    prefix="crew-session",
    owner_name="kyleseaman",
    deployment_id="deployment-a",
)


def _allocate(registry, session_key="dashboard:one", **overrides) -> WorkspaceBinding:
    return registry.allocate(session_key, **{**_ALLOCATE_DEFAULTS, **overrides})


def _write_raw(path: Path, bindings: dict[str, object], *, version: object = 1) -> None:
    """Seed a registry file directly, to exercise the corruption arms."""
    payload = {"version": version, "bindings": bindings}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "coder_workspaces.json"


def test_allocate_is_idempotent_for_one_parent_and_distinct_between_parents(path):
    registry = WorkspaceBindingRegistry(path)
    first = _allocate(registry, "dashboard:one", preset="arm-small")
    again = _allocate(registry, "dashboard:one", preset="arm-small")
    other = _allocate(registry, "dashboard:two", preset="arm-small")
    assert again == first
    assert other.binding_id != first.binding_id
    assert other.workspace_name != first.workspace_name
    assert first.workspace_name.startswith("crew-session-kyleseaman-")
    assert len(first.workspace_name) <= 32
    assert "dashboard" not in first.workspace_name
    assert "one" not in first.workspace_name
    assert _CODER_NAME_RE.fullmatch(first.workspace_name)


def test_a_fresh_binding_records_deployment_before_workspace_creation(path):
    binding = _allocate(WorkspaceBindingRegistry(path))
    assert binding.state == "allocated"
    assert binding.generation == 1
    assert binding.workspace_uuid == ""
    assert binding.owner_id == ""
    assert binding.organization_id == ""
    assert binding.deployment_id == "deployment-a"
    assert binding.deleted_at == ""
    assert binding.failure_code == ""
    assert binding.created_at == binding.last_activity_at
    assert binding.created_at.endswith("Z")


def test_owner_name_with_email_shape_is_not_exposed_in_workspace_name(path):
    registry = WorkspaceBindingRegistry(path)
    binding = _allocate(registry, owner_name="operator@example.com")
    assert binding.workspace_name.startswith("crew-session-user-")
    assert "operator" not in binding.workspace_name
    assert "example" not in binding.workspace_name


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"session_key": ""}, "session_key is required"),
        ({"template": "not a name"}, "template must be one safe Coder name"),
        ({"template": "-leading"}, "template must be one safe Coder name"),
        ({"preset": "not a name"}, "preset must be one safe Coder name"),
        ({"prefix": "Crew-Session"}, "prefix must be one safe Coder name"),
        ({"prefix": "crew_session"}, "prefix must be one safe Coder name"),
        ({"owner_name": ""}, "owner_name is required"),
    ),
)
def test_allocate_refuses_an_unusable_argument_before_writing(path, kwargs, message):
    registry = WorkspaceBindingRegistry(path)
    with pytest.raises(ValueError, match=re.escape(message)):
        _allocate(registry, **kwargs)  # type: ignore[arg-type]

    assert not path.exists()


def test_the_prefix_cap_is_the_widest_prefix_that_still_mints_a_legal_name(path):
    registry = WorkspaceBindingRegistry(path)
    at_cap = "c" * CODER_WORKSPACE_PREFIX_MAX_CHARS
    binding = _allocate(registry, prefix=at_cap)
    assert _CODER_NAME_RE.fullmatch(binding.workspace_name)
    assert len(binding.workspace_name) <= 32
    with pytest.raises(ValueError, match="at most"):
        _allocate(registry, "dashboard:two", prefix=at_cap + "c")


def test_identity_allocation_fails_closed_after_bounded_collisions(path, monkeypatch):
    registry = WorkspaceBindingRegistry(path)
    _allocate(registry, "dashboard:one")
    monkeypatch.setattr(registry_mod.secrets, "token_bytes", lambda _size: b"\0" * 5)
    first_collision = _allocate(registry, "dashboard:collision-seed")
    assert first_collision.binding_id == "aaaaaaaa"
    with pytest.raises(WorkspaceRegistryCorrupt, match="unique Coder workspace identity"):
        _allocate(registry, "dashboard:two")


def test_registry_round_trips_through_a_second_instance(path):
    created = _allocate(WorkspaceBindingRegistry(path), "cron:job:run")
    loaded = WorkspaceBindingRegistry(path).get_by_session("cron:job:run")
    assert loaded == created
    assert WorkspaceBindingRegistry(path).get(created.binding_id) == created
    assert WorkspaceBindingRegistry(path).get_by_session("cron:absent") is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["bindings"][created.binding_id]["workspace_uuid"] == ""


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_the_registry_file_is_written_owner_only(path):

    _allocate(WorkspaceBindingRegistry(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unchanged_registry_is_parsed_once_per_registry_instance(path, monkeypatch):
    created = _allocate(WorkspaceBindingRegistry(path))
    loads = 0

    def counting_loads(value: str) -> object:
        nonlocal loads
        loads += 1
        return json.loads(value)

    # Patch the module's name, not stdlib json: other threads decode too.
    monkeypatch.setattr(
        registry_mod,
        "json",
        SimpleNamespace(loads=counting_loads, dumps=json.dumps, JSONDecodeError=ValueError),
    )
    registry = WorkspaceBindingRegistry(path)
    assert registry.get(created.binding_id) == created
    assert registry.get_by_session("dashboard:one") == created
    assert registry.list_bindings() == (created,)
    assert loads == 1


def test_an_absent_registry_reads_as_empty_without_creating_it(tmp_path):
    path = tmp_path / "nested" / "coder_workspaces.json"
    assert WorkspaceBindingRegistry(path).list_bindings() == ()
    assert not path.exists()


def test_a_second_writer_on_one_path_does_not_clobber_the_first(path):
    """Every write re-reads under the lock, so a stale cache cannot drop a row."""
    first = WorkspaceBindingRegistry(path)
    second = WorkspaceBindingRegistry(path)
    one = _allocate(first, "dashboard:one")
    two = _allocate(second, "dashboard:two")
    three = _allocate(first, "dashboard:three")

    # Keyed by id: list order is not part of the contract.
    expected = {item.binding_id: item for item in (one, two, three)}
    assert {item.binding_id: item for item in first.list_bindings()} == expected
    assert {item.binding_id: item for item in second.list_bindings()} == expected
    assert len({item.workspace_name for item in first.list_bindings()}) == 3
    assert len({item.session_key for item in first.list_bindings()}) == 3


def test_the_lock_lives_beside_the_registry_and_is_not_the_registry(tmp_path, path):
    registry = WorkspaceBindingRegistry(path)
    _allocate(registry)
    assert registry.lock_path == tmp_path / "coder_workspaces.lock"
    assert registry.lock_path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["bindings"]


def test_replace_persists_lifecycle_fields_for_the_same_identity(path):
    registry = WorkspaceBindingRegistry(path)
    binding = _allocate(registry)
    provisioned = registry.replace(
        replace(
            binding,
            workspace_uuid="wsuuid-1",
            owner_id="owner-1",
            organization_id="org-1",
            state="running",
            last_activity_at="2026-09-28T00:00:00Z",
            deletion_due_at="2026-10-28T00:00:00Z",
            failure_code="",
        )
    )
    assert provisioned.workspace_uuid == "wsuuid-1"
    assert provisioned.owner_id == "owner-1"
    assert provisioned.state == "running"
    assert WorkspaceBindingRegistry(path).get(binding.binding_id) == provisioned


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("session_key", "dashboard:someone-else", "binding identity changed"),
        ("workspace_name", "crew-session-elsewhere-aaaaaaaa", "binding identity changed"),
        ("deployment_id", "other-deployment", "deployment identity may not be retargeted"),
    ),
)
def test_replace_refuses_a_rewritten_identity_or_provenance(path, field, value, message):
    registry = WorkspaceBindingRegistry(path)
    binding = _allocate(registry)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(WorkspaceRegistryCorrupt, match=message):
        registry.replace(replace(binding, **{field: value}))

    assert path.read_text(encoding="utf-8") == before
    assert WorkspaceBindingRegistry(path).get(binding.binding_id) == binding


def test_replace_refuses_a_binding_the_registry_does_not_hold(path):
    registry = WorkspaceBindingRegistry(path)
    binding = _allocate(registry)
    unknown = replace(binding, binding_id="bbbbbbbb")
    with pytest.raises(WorkspaceRegistryCorrupt, match="binding disappeared"):
        registry.replace(unknown)

    assert registry.get("bbbbbbbb") is None


def test_corrupt_registry_fails_closed_without_overwrite(path):
    original = "{not-json"
    path.write_text(original, encoding="utf-8")
    registry = WorkspaceBindingRegistry(path)
    with pytest.raises(WorkspaceRegistryCorrupt, match="registry is unreadable"):
        _allocate(registry)

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"version": 2, "bindings": {}}', "registry version is invalid"),
        ('{"bindings": {}}', "registry version is invalid"),
        ('["version", 1]', "registry version is invalid"),
        ('{"version": 1, "bindings": []}', "registry bindings are invalid"),
        ('{"version": 1, "bindings": {"aaaaaaaa": 7}}', "binding is not an object"),
    ),
)
def test_a_registry_whose_envelope_is_wrong_fails_closed(path, payload, message):
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(WorkspaceRegistryCorrupt, match=message):
        WorkspaceBindingRegistry(path).list_bindings()

    assert path.read_text(encoding="utf-8") == payload


def test_a_binding_missing_an_original_field_fails_closed(path):
    seed = asdict(_allocate(WorkspaceBindingRegistry(path)))
    del seed["template"]
    _write_raw(path, {seed["binding_id"]: seed})
    with pytest.raises(WorkspaceRegistryCorrupt, match="binding has an invalid shape"):
        WorkspaceBindingRegistry(path).list_bindings()


@pytest.mark.parametrize(
    "overrides",
    (
        {"binding_id": ""},
        {"session_key": ""},
        {"workspace_name": "Crew Session"},
        {"workspace_name": "-leading"},
        {"generation": 0},
        {"generation": "1"},
        {"generation": None},
    ),
)
def test_a_binding_with_invalid_identity_fields_fails_closed(path, overrides):
    seed = asdict(_allocate(WorkspaceBindingRegistry(path)))
    key = seed["binding_id"]
    seed.update(overrides)
    _write_raw(path, {key: seed})
    with pytest.raises(WorkspaceRegistryCorrupt, match="invalid identity fields"):
        WorkspaceBindingRegistry(path).list_bindings()


def test_a_key_that_does_not_name_its_own_binding_fails_closed(path):
    seed = asdict(_allocate(WorkspaceBindingRegistry(path)))
    _write_raw(path, {"bbbbbbbb": seed})
    with pytest.raises(WorkspaceRegistryCorrupt, match="identity is ambiguous"):
        WorkspaceBindingRegistry(path).list_bindings()


def test_two_bindings_claiming_one_parent_session_fail_closed(path):
    """One workspace per trusted parent session, enforced on READ as well as write."""
    first = _allocate(WorkspaceBindingRegistry(path), "dashboard:one")
    duplicate = replace(
        first, binding_id="bbbbbbbb", workspace_name="crew-session-kyleseaman-bbbbbbbb"
    )
    _write_raw(path, {first.binding_id: asdict(first), duplicate.binding_id: asdict(duplicate)})
    with pytest.raises(WorkspaceRegistryCorrupt, match="identity is ambiguous"):
        WorkspaceBindingRegistry(path).list_bindings()


def test_an_unknown_stored_field_is_dropped_rather_than_failing_closed(path):
    """A newer build's extra field is ignored: quarantine would forget workspaces."""
    seed = asdict(_allocate(WorkspaceBindingRegistry(path), "dashboard:one"))
    key = seed["binding_id"]
    seed["some_future_field"] = "from-a-newer-build"
    _write_raw(path, {key: seed})
    loaded = WorkspaceBindingRegistry(path).get(key)
    assert loaded is not None
    assert loaded.session_key == "dashboard:one"
    assert not hasattr(loaded, "some_future_field")


@pytest.mark.parametrize("stored_deployment", ["deployment-a", ""])
def test_allocate_refuses_reuse_across_deployments_without_changing_the_record(
    path, stored_deployment
):
    binding = _allocate(WorkspaceBindingRegistry(path))
    stored = replace(binding, deployment_id=stored_deployment)
    _write_raw(path, {stored.binding_id: asdict(stored)})
    before = path.read_bytes()
    registry = WorkspaceBindingRegistry(path)
    with pytest.raises(WorkspaceRegistryCorrupt, match="deployment identity"):
        _allocate(registry, deployment_id="deployment-b")

    assert path.read_bytes() == before
    assert registry.get(binding.binding_id) == stored


@pytest.mark.parametrize("deployment_id", ["", " ", None, 17])
def test_allocate_requires_deployment_identity_before_writing(path, deployment_id):

    with pytest.raises(ValueError, match="deployment_id"):
        _allocate(WorkspaceBindingRegistry(path), deployment_id=deployment_id)  # type: ignore[arg-type]

    assert not path.exists()
