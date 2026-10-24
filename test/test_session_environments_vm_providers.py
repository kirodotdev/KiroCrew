"""Sprites and boxd profiles beside Coder and Kiro Cloud: one id names one
provider across every list, a stale card keeps the lists it never showed, and
launch, the picker and retention all read the same provider table."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_session_environments_config import _BASE_CONFIG, _profile, _stored
from test_session_environments_config_api import _api, _app, _patch

from kiro_crew.config import session_environments as se


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


# Cross-provider ids address both the picker and the launch resolver.
_PROVIDER_KEYS = ("profiles", "sprites_profiles", "boxd_profiles")


def _provider_section():
    return {
        "coder_url": "https://c.example.com",
        "profiles": [_profile("coder-id")],
        "sprites_profiles": [
            {"id": "sprites-id", "name": "Sprite", "token_secret": "TOKEN", "workdir": "/w"}
        ],
        "boxd_profiles": [
            {"id": "boxd-id", "name": "Box", "snapshot": "", "vcpu": 0, "workdir": "/w"}
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed,existing,whole",
    [
        (a, b, whole)
        for a in _PROVIDER_KEYS
        for b in _PROVIDER_KEYS
        for whole in (False, True)
        # Coder profiles have no leaf path: only the whole section writes them.
        if a != b and (whole or a != "profiles")
    ],
)
async def test_cross_provider_collision_is_refused_under_write_lock(
    config_file, monkeypatch, changed, existing, whole
):
    from kiro_crew.config import loader

    section = _provider_section()
    config_file.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}))
    colliding = [{**section[changed][0], "id": section[existing][0]["id"]}]
    checked = []
    original = loader.update_config_locked

    def locked(path, *, mutate, **kwargs):
        def checking(data):
            checked.append(True)
            return mutate(data)

        return original(path, mutate=checking, **kwargs)

    monkeypatch.setattr(loader, "update_config_locked", locked)
    before = config_file.read_bytes()
    # Whole-section clients may omit the other providers; the locked merge must
    # still check their preserved lists. Coder keys are required in a whole save.
    value = {"coder_url": section["coder_url"], "profiles": section["profiles"]}
    value[changed] = colliding
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(
            client,
            "session_environments" if whole else f"session_environments.{changed}",
            value if whole else colliding,
        )
        assert resp.status == 400, await resp.text()
        assert "both" in (await resp.json())["error"]
    assert config_file.read_bytes() == before
    if not whole or existing != "profiles":
        assert checked == [True]


@pytest.mark.parametrize(
    "left,right",
    [
        ("profiles", "sprites_profiles"),
        ("profiles", "boxd_profiles"),
        ("sprites_profiles", "boxd_profiles"),
    ],
)
@pytest.mark.parametrize("coder_url", ["", "https://c.example.com"])
def test_hand_edited_collision_stays_unavailable_to_picker_and_launch(
    monkeypatch, left, right, coder_url
):
    from types import SimpleNamespace

    from kiro_crew.coder.session_launch import profile_provider
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.dashboard.handlers import session_environments as handlers
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw = _provider_section()
    raw["coder_url"] = coder_url
    raw[right][0]["id"] = raw[left][0]["id"]
    section = SessionEnvironmentsConfig.from_raw(raw)
    monkeypatch.setattr(
        KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=section)
    )
    duplicate = raw[left][0]["id"]
    with pytest.raises(SessionEnvironmentUnavailable, match="more than one provider"):
        profile_provider(section, duplicate)
    offered = handlers._profiles()
    assert duplicate not in {p["id"] for p in offered}
    assert all(profile_provider(section, p["id"]) == p["provider"] for p in offered)


@pytest.mark.parametrize(
    "key,provider",
    [("profiles", "coder"), ("sprites_profiles", "sprites"), ("boxd_profiles", "boxd")],
)
def test_choice_and_inherited_rows_record_the_resolved_provider(
    tmp_path, monkeypatch, key, provider
):
    from types import SimpleNamespace

    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.config import live
    from kiro_crew.config.sections import SessionEnvironmentsConfig

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    choices = SessionEnvironmentChoices(tmp_path / "choices.json")
    profile_id = raw[key][0]["id"]
    choices.set("parent", profile_id, provider)
    assert choices.inherit("child", "parent") == profile_id
    rows = json.loads(choices.path.read_text())["sessions"]
    assert rows["parent"] == {"provider": provider, "configuration": profile_id}
    assert rows["child"] == {"provider": provider, "configuration": profile_id, "parent": "parent"}
    assert choices.resolve("child") == (profile_id, "parent")


def _launch_fixture(tmp_path, monkeypatch):
    """A live config of :func:`_provider_section` and an empty data home."""
    from types import SimpleNamespace

    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths
    from kiro_crew.config.sections import SessionEnvironmentsConfig

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")

    def reread_config() -> None:
        config.session_environments = SessionEnvironmentsConfig.from_raw(raw)

    return raw, choices, reread_config


@pytest.mark.asyncio
async def test_launch_dispatches_by_id_and_refuses_collisions(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.coder import session_launch
    from kiro_crew.session_environment import SessionEnvironmentUnavailable
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    raw, choices, reread_config = _launch_fixture(tmp_path, monkeypatch)
    choices.set("session", "sprites-id", "sprites")
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(SpritesSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("session") == "prepared"
    prepare.assert_awaited_once_with("session", "sprites-id", progress_key="session")
    raw["profiles"][0]["id"] = "sprites-id"
    reread_config()
    with pytest.raises(SessionEnvironmentUnavailable, match="more than one provider"):
        await session_launch.prepare_for_session("session")
    assert prepare.await_count == 1


@pytest.mark.asyncio
async def test_launch_refuses_a_row_whose_profile_id_moved_to_another_provider(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw, choices, reread_config = _launch_fixture(tmp_path, monkeypatch)
    choices.set("session", "sprites-id", "sprites")
    # The Sprites profile is removed and a boxd profile reuses its id.
    raw["sprites_profiles"] = []
    raw["boxd_profiles"][0]["id"] = "sprites-id"
    reread_config()
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(BoxdSessionEnvironments, "prepare", prepare)
    with pytest.raises(SessionEnvironmentUnavailable, match="now names a boxd profile"):
        await session_launch.prepare_for_session("session")
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_removed_vm_profile_is_refused_rather_than_run_on_coder(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.coder import session_launch
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw, choices, reread_config = _launch_fixture(tmp_path, monkeypatch)
    choices.set("session", "sprites-id", "sprites")
    raw["sprites_profiles"] = []
    reread_config()
    coder_prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "prepare", coder_prepare)
    with pytest.raises(SessionEnvironmentUnavailable, match="no longer configured"):
        await session_launch.prepare_for_session("session")
    coder_prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_subagent_inherits_its_roots_recorded_provider(tmp_path, monkeypatch):
    """A root tagged sprites whose id now names boxd: its subagent is refused too."""
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw, choices, reread_config = _launch_fixture(tmp_path, monkeypatch)
    choices.set("root", "sprites-id", "sprites")
    # The Sprites profile is replaced by a boxd profile with the same id.
    raw["sprites_profiles"] = []
    raw["boxd_profiles"][0]["id"] = "sprites-id"
    reread_config()
    assert choices.inherit("child", "root") == "sprites-id"
    assert choices.resolve_choice("child") == ("sprites-id", "root", "sprites")
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(BoxdSessionEnvironments, "prepare", prepare)
    for key in ("root", "child"):
        with pytest.raises(SessionEnvironmentUnavailable, match="now names a boxd profile"):
            await session_launch.prepare_for_session(key)
    prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owned,now",
    [("sprites", "boxd"), ("boxd", "sprites"), ("sprites", "coder"), ("boxd", "coder")],
)
async def test_a_coder_tagged_session_holding_a_vm_never_moves_to_a_reused_id(
    tmp_path, monkeypatch, owned, now
):
    """A choice tagged "coder" whose session already holds a sprite or boxd machine
    is pinned to it, even when the id names a Coder profile again: moving it would
    leave the archived VM's deletion stamp standing and start an empty one."""
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.coder.persistent_vm import SessionResource, SessionResourceRegistry
    from kiro_crew.session_environment import SessionEnvironmentUnavailable
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    classes = {"sprites": SpritesSessionEnvironments, "boxd": BoxdSessionEnvironments}
    raw, choices, reread_config = _launch_fixture(tmp_path, monkeypatch)
    choices.set("session", "coder-id", "coder")
    # The session ran on the other provider under the reused id, and was archived.
    cls = classes[owned]
    registry = SessionResourceRegistry(tmp_path / cls.registry_file, cls.label)
    registry.put(SessionResource("session", "coder-id", "kc-0123456789abcdef", "vm-id"))
    registry.set_deletion("session", "2099-01-01T00:00:00Z")
    # Now the id names a third provider (or Coder again).
    raw["profiles"] = [_profile("coder-id")] if now == "coder" else []
    if now != "coder":
        raw[f"{now}_profiles"][0]["id"] = "coder-id"
    reread_config()
    prepares = {}
    for name, provider_cls in classes.items():
        prepares[name] = AsyncMock(return_value="prepared")
        monkeypatch.setattr(provider_cls, "prepare", prepares[name])
    coder_prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "prepare", coder_prepare)
    # The row's own tag refuses the move to a VM; the owned VM refuses the Coder one.
    with pytest.raises(SessionEnvironmentUnavailable, match="now names a|another provider"):
        await session_launch.prepare_for_session("session")
    for mock in (*prepares.values(), coder_prepare):
        mock.assert_not_awaited()
    assert registry.get("session").deletion_due_at == "2099-01-01T00:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", _PROVIDER_KEYS)
@pytest.mark.parametrize("malformed", [None, [None], ["not-an-object"], [{}], [{"id": 7}]])
async def test_leaf_patch_ignores_malformed_stored_provider_rows(config_file, key, malformed):
    section = _provider_section()
    section[key] = malformed
    config_file.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}))
    async with TestClient(TestServer(_app())) as client:
        response = await _patch(client, "session_environments.stop_on_shutdown", False)
        assert response.status == 200, await response.text()
        assert (await response.json())["session_environments"]["stop_on_shutdown"] is False
    saved = _stored(config_file)["session_environments"]
    assert saved[key] == malformed
    assert saved["stop_on_shutdown"] is False


@pytest.mark.asyncio
async def test_a_stale_coder_only_save_keeps_the_sprites_and_boxd_profiles(config_file):
    """A Settings tab or change card that only knows Coder saves the section without
    the VM lists; the profiles it never showed are kept, not wiped."""
    seeded = {**_provider_section(), "default_profile": "boxd-id"}
    stale = {"coder_url": seeded["coder_url"], "profiles": seeded["profiles"]}
    async with _api() as client:
        assert (await _patch(client, "session_environments", seeded)).status == 200
        resp = await _patch(client, "session_environments", stale)
        assert resp.status == 200, await resp.text()
    saved = _stored(config_file)["session_environments"]
    assert saved["sprites_profiles"] == seeded["sprites_profiles"]
    assert saved["boxd_profiles"] == seeded["boxd_profiles"]
    assert saved["default_profile"] == "boxd-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ("sprites_profiles", "boxd_profiles"))
@pytest.mark.parametrize(
    "malformed",
    ["not-an-object", {"id": "Not Valid!"}, "duplicate"],
    ids=["not-an-object", "bad-entry", "duplicate-id"],
)
async def test_one_hand_edited_vm_entry_does_not_drop_the_valid_ones(config_file, key, malformed):
    """The omitted list is carried as the loader reads it: entry by entry."""
    section = {**_provider_section(), "default_profile": f"{key.split('_')[0]}-id"}
    valid = section[key][0]
    section[key] = [
        valid,
        {**valid, "name": "Duplicate"} if malformed == "duplicate" else malformed,
    ]
    config_file.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}))
    stale = {"coder_url": section["coder_url"], "profiles": section["profiles"]}
    async with _api() as client:
        resp = await _patch(client, "session_environments", stale)
        assert resp.status == 200, await resp.text()
    saved = _stored(config_file)["session_environments"]
    assert saved[key] == [valid]
    assert saved["default_profile"] == section["default_profile"]


def test_every_vm_provider_is_wired_to_launch_and_the_move_guard():
    """One table names the VM providers; launch and the move guard read the same
    wiring, so a provider listed in one but not the other fails here."""
    from kiro_crew.coder import session_launch

    wired = session_launch._vm_environments()
    assert set(wired) == {provider for provider, _key, _label in se.VM_PROVIDERS}
    assert set(se.PROVIDER_IDS) == {
        session_launch.PROVIDER_ID,
        session_launch.KIRO_CLOUD_PROVIDER_ID,
        *wired,
    }
    assert session_launch.CHOICE_PROVIDERS == frozenset(se.PROVIDER_IDS)
    # The boot check reads VM_REGISTRY_FILES; each class uses the same path.
    assert set(se.VM_REGISTRY_FILES) == set(wired)
    for provider, cls in wired.items():
        assert cls.registry_file == se.VM_REGISTRY_FILES[provider], provider


@pytest.mark.asyncio
async def test_the_sweep_and_shutdown_cover_every_vm_provider(monkeypatch, tmp_path):
    """Retention builds its provider list from the same table, so a provider
    added there can never miss the sweep (its archived VMs would keep billing)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.coder import retention, session_launch
    from kiro_crew.config import paths

    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    built = await retention._persistent_environments()
    assert [type(e) for e in built] == list(session_launch._vm_environments().values())


@pytest.mark.asyncio
async def test_with_the_developer_flag_off_no_sprites_or_boxd_path_is_reachable(
    tmp_path, monkeypatch
):
    """``KIROCREW_DEV_MODE`` off: with Sprites and boxd profiles saved and a session
    bound to one, the picker offers neither, a start runs locally, retention builds
    no VM provider and boot starts no sweeper. Each half fails if its guard goes."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.coder import retention, session_launch
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.dashboard.handlers import session_environments as handlers
    from kiro_crew.slack import gateway
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    raw, choices, _reread_config = _launch_fixture(tmp_path, monkeypatch)
    raw["coder_url"] = ""
    raw["profiles"] = []
    _reread_config()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=config.session_environments))
    choices.set("dashboard:vm", "sprites-id", "sprites")
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(SpritesSessionEnvironments, "prepare", prepare)
    sweeper = AsyncMock()
    monkeypatch.setattr(retention, "run_sweeper", sweeper)

    # Flag on: every path is live, so the assertions below test the flag.
    assert {p["provider"] for p in handlers._profiles()} == {"sprites", "boxd"}
    assert await session_launch.prepare_for_session("dashboard:vm") == "prepared"
    assert len(await retention._persistent_environments()) == 2

    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert handlers._profiles() == []
    assert await session_launch.prepare_for_session("dashboard:vm") is None
    assert prepare.await_count == 1
    assert await retention._persistent_environments() == []
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    gateway._start_coder_retention(config)
    assert gateway._CODER_RETENTION_TASK is None
    sweeper.assert_not_awaited()
