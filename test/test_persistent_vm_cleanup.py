"""Shutdown and retention only act on owned persistent machines."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
from kiro_crew.coder import retention
from kiro_crew.config import paths
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.session_environments import persistent_vm
from kiro_crew.session_environments.persistent_vm import SessionResource, stamp
from kiro_crew.sprites_session import launch as sprites_launch
from kiro_crew.sprites_session.launch import SpritesSessionEnvironments


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NAME = "kc-0123456789abcdef"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_id", [None, "replacement-id", "owned-id"])
async def test_shutdown_checks_live_boxd_identity_before_hibernating(
    tmp_path, monkeypatch, live_id
):
    section = SimpleNamespace(boxd_profiles=[{"id": "box"}])
    environments = BoxdSessionEnvironments(section, tmp_path)
    row = SessionResource("session", "box", NAME, "owned-id")
    environments.registry.put(row)
    cli = SimpleNamespace(
        identity=AsyncMock(return_value="acct"),
        get=AsyncMock(return_value={"id": live_id} if live_id else None),
        hibernate=AsyncMock(),
    )
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=section))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(BoxdSessionEnvironments, "cli", AsyncMock(return_value=cli))
    counts = await retention.stop_for_shutdown()
    cli.get.assert_awaited_once_with(NAME)
    if live_id == "owned-id":
        cli.hibernate.assert_awaited_once_with(NAME)
        assert counts["boxd"] == 1
    else:
        cli.hibernate.assert_not_awaited()
        assert counts["boxd"] == 0
    assert environments.registry.get("session") == row


@pytest.mark.asyncio
async def test_shutdown_hibernates_boxd_machines_in_parallel(tmp_path, monkeypatch):
    """One at a time, the later machines would outlast the shutdown budget."""
    import asyncio

    from kiro_crew.testing.wait import await_until

    section = SimpleNamespace(boxd_profiles=[{"id": "box"}])
    environments = BoxdSessionEnvironments(section, tmp_path)
    names = [f"kc-{i:016x}" for i in range(3)]
    for i, name in enumerate(names):
        environments.registry.put(SessionResource(f"session-{i}", "box", name, f"id-{i}"))
    started: list[str] = []
    all_started, release = asyncio.Event(), asyncio.Event()

    async def hibernate(name):
        started.append(name)
        if len(started) == len(names):
            all_started.set()
        await release.wait()

    async def get(name):
        return {"id": f"id-{names.index(name)}"}

    cli = SimpleNamespace(identity=AsyncMock(return_value="acct"), get=get, hibernate=hibernate)
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=section))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(BoxdSessionEnvironments, "cli", AsyncMock(return_value=cli))
    stop = asyncio.create_task(retention.stop_for_shutdown())
    try:
        # Every hibernate is in flight before any one finishes.
        await await_until(all_started, what="all three hibernates in flight")
    finally:
        release.set()
    assert (await stop)["boxd"] == 3
    assert sorted(started) == sorted(names)


@pytest.mark.asyncio
async def test_shutdown_continues_after_one_boxd_identity_lookup_fails(tmp_path, monkeypatch):
    section = SimpleNamespace(boxd_profiles=[{"id": "box"}])
    environments = BoxdSessionEnvironments(section, tmp_path)
    first = SessionResource("first", "box", NAME, "id-first")
    second = SessionResource("second", "box", "kc-fedcba9876543210", "id-second")
    for row in (first, second):
        environments.registry.put(row)
    cli = SimpleNamespace(
        identity=AsyncMock(return_value="acct"),
        get=AsyncMock(side_effect=[SessionEnvironmentUnavailable("offline"), {"id": "id-second"}]),
        hibernate=AsyncMock(),
    )
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=section))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(BoxdSessionEnvironments, "cli", AsyncMock(return_value=cli))
    counts = await retention.stop_for_shutdown()
    assert cli.get.await_count == 2
    cli.hibernate.assert_awaited_once_with(second.name)
    assert counts["boxd"] == 1
    assert environments.registry.all() == [first, second]


@pytest.mark.asyncio
async def test_removed_sprites_profile_names_the_repair_and_keeps_the_due_row(tmp_path):
    original = {"id": "fly", "token_secret": "ORIGINAL_TOKEN"}
    section = SimpleNamespace(sprites_profiles=[])
    tokens_read = []
    environments = SpritesSessionEnvironments(
        section, tmp_path, token_reader=lambda name: tokens_read.append(name) or "example-token"
    )
    row = SessionResource("session", "fly", "kirocrew-0123456789abcdef", "sprite-id")
    environments.registry.put(row)
    environments.registry.set_deletion(row.session_key, stamp(NOW))
    with pytest.raises(SessionEnvironmentUnavailable, match="restore the original profile"):
        await environments._delete(row)
    assert environments.registry.get(row.session_key).deletion_due_at == stamp(NOW)
    assert tokens_read == []
    # The suggested repair must actually unblock deletion with the original secret.
    section.sprites_profiles = [original]
    from unittest.mock import patch

    client = SimpleNamespace(
        get_sprite=AsyncMock(return_value={"id": "sprite-id"}), delete_sprite=AsyncMock()
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        assert await environments.sweep_due(now=NOW) == 1
    assert tokens_read == ["ORIGINAL_TOKEN", "ORIGINAL_TOKEN"]
    client.delete_sprite.assert_awaited_once_with(row.name)
    assert environments.registry.get(row.session_key) is None


@pytest.mark.asyncio
async def test_a_sprite_whose_profile_was_removed_is_still_deleted_when_due(tmp_path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "ORIGINAL_TOKEN"}
    section = SimpleNamespace(sprites_profiles=[profile])
    tokens_read = []
    environments = SpritesSessionEnvironments(
        section, tmp_path, token_reader=lambda name: tokens_read.append(name) or "example-token"
    )
    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        delete_sprite=AsyncMock(),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        row = await environments._create("session", profile, "session")
        environments.registry.set_deletion(row.session_key, stamp(NOW))
        section.sprites_profiles = []  # the user removes the profile
        assert await environments.sweep_due(now=NOW) == 1
    client.delete_sprite.assert_awaited_once_with(row.name)
    assert environments.registry.get(row.session_key) is None
    assert set(tokens_read) == {"ORIGINAL_TOKEN"}


@pytest.mark.asyncio
async def test_a_changed_token_key_never_replaces_the_sessions_sprite(tmp_path, monkeypatch):
    """Under another key the sprite's name would 404; that must not read as "deleted"."""
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "ORIGINAL_TOKEN", "workdir": ""}
    section = SimpleNamespace(sprites_profiles=[profile])
    tokens_read = []
    environments = SpritesSessionEnvironments(
        section, tmp_path, token_reader=lambda name: tokens_read.append(name) or "example-token"
    )
    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(return_value=None),  # another organization: not found
        delete_sprite=AsyncMock(),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        row = await environments._create("session", profile, "session")
        client.create_sprite.reset_mock()
        tokens_read.clear()
        section.sprites_profiles = [{**profile, "token_secret": "OTHER_ORG_TOKEN"}]
        with pytest.raises(SessionEnvironmentUnavailable, match="ORIGINAL_TOKEN"):
            await environments.prepare("session", "fly")
        with pytest.raises(SessionEnvironmentUnavailable, match="ORIGINAL_TOKEN"):
            await environments.existing("session", "fly")
        client.create_sprite.assert_not_awaited()
        assert environments.registry.get("session") == row
        # Retention still reaches it with the key it was created with.
        tokens_read.clear()
        client.get_sprite.return_value = {"id": "sprite-id"}
        environments.registry.set_deletion("session", stamp(NOW))
        assert await environments.sweep_due(now=NOW) == 1
    client.delete_sprite.assert_awaited_once_with(row.name)
    assert set(tokens_read) == {"ORIGINAL_TOKEN"}


@pytest.mark.asyncio
async def test_another_accounts_token_under_the_same_key_never_replaces_or_forgets_it(
    tmp_path, monkeypatch
):
    """Same secret name, different token: a 404 is another account, not a deletion."""
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "SPRITES_TOKEN", "workdir": ""}
    section = SimpleNamespace(sprites_profiles=[profile])
    vault = {"SPRITES_TOKEN": "org-a-token"}
    environments = SpritesSessionEnvironments(section, tmp_path, token_reader=vault.get)
    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(return_value=None),
        delete_sprite=AsyncMock(),
        # A blank working folder: the start reads the guest's login home.
        exec=AsyncMock(return_value=(0, b"/guest", b"")),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        row = await environments._create("session", profile, "session")
        assert row.credential_id and "org-a-token" not in json.dumps(
            json.loads(environments.registry.path.read_text())
        )
        client.create_sprite.reset_mock()
        vault["SPRITES_TOKEN"] = "org-b-token"  # another organization's token
        with pytest.raises(SessionEnvironmentUnavailable, match="nothing was deleted"):
            await environments.prepare("session", "fly")
        client.create_sprite.assert_not_awaited()
        assert environments.registry.get("session") == row
        # Retention does not read the miss as a deletion either.
        environments.registry.set_deletion("session", stamp(NOW))
        assert await environments.sweep_due(now=NOW) == 0
        client.delete_sprite.assert_not_awaited()
        assert environments.registry.get("session").name == row.name
    # The same miss under the original token is a real deletion: replaced as before.
    vault["SPRITES_TOKEN"] = "org-a-token"
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        environments.registry.set_deletion("session", "")
        prepared = await environments.prepare("session", "fly")
    assert prepared.created is True
    assert environments.registry.get("session").name != row.name


@pytest.mark.asyncio
async def test_a_token_saved_during_a_lookup_is_not_credited_with_its_answer(tmp_path, monkeypatch):
    """Account B's token saved while A's lookup runs: A's answer is recorded as A's."""
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "SPRITES_TOKEN", "workdir": ""}
    section = SimpleNamespace(sprites_profiles=[profile])
    vault = {"SPRITES_TOKEN": "org-a-token"}
    environments = SpritesSessionEnvironments(section, tmp_path, token_reader=vault.get)

    async def found_while_b_is_saved(_name):
        vault["SPRITES_TOKEN"] = "org-b-token"  # lands mid-request
        return {"id": "sprite-id"}

    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(side_effect=found_while_b_is_saved),
        delete_sprite=AsyncMock(),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        row = await environments._create("session", profile, "session")
        # B's token is what a shell would now be built from: nothing runs on it.
        with pytest.raises(SessionEnvironmentUnavailable, match="changed after its identity"):
            await environments.prepare("session", "fly")
        assert environments.registry.get("session").credential_id == row.credential_id
        client.get_sprite.side_effect = None
        client.get_sprite.return_value = None  # B's account does not have it
        client.create_sprite.reset_mock()
        with pytest.raises(SessionEnvironmentUnavailable, match="nothing was deleted"):
            await environments.prepare("session", "fly")
    client.create_sprite.assert_not_awaited()
    assert environments.registry.get("session").name == row.name


@pytest.mark.asyncio
async def test_a_token_saved_after_the_identity_check_never_runs_a_command(tmp_path, monkeypatch):
    """B's token saved between A's lookup and the shell: no exec, under either token."""
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "SPRITES_TOKEN", "workdir": "/w"}
    section = SimpleNamespace(sprites_profiles=[profile])
    vault = {"SPRITES_TOKEN": "org-a-token"}
    environments = SpritesSessionEnvironments(section, tmp_path, token_reader=vault.get)

    async def found_then_b_is_saved(_name):
        vault["SPRITES_TOKEN"] = "org-b-token"  # lands after the answer, before the shell
        return {"id": "sprite-id"}

    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(side_effect=found_then_b_is_saved),
        delete_sprite=AsyncMock(),
        exec=AsyncMock(return_value=(0, b"", b"")),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        environments.registry.put(
            SessionResource(
                "session",
                "fly",
                "kirocrew-0123456789abcdef",
                "sprite-id",
                credential_id=sprites_launch._fingerprint("org-a-token"),
                token_secret="SPRITES_TOKEN",
            )
        )
        with pytest.raises(SessionEnvironmentUnavailable, match="nothing was run"):
            await environments.prepare("session", "fly")
    client.exec.assert_not_awaited()
    client.create_sprite.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_token_saved_during_the_sweeps_lookup_never_sends_its_delete(tmp_path, monkeypatch):
    """B's token saved while the sweep checks A's sprite: no DELETE, and the row stays."""
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "SPRITES_TOKEN", "workdir": ""}
    section = SimpleNamespace(sprites_profiles=[profile])
    vault = {"SPRITES_TOKEN": "org-a-token"}
    environments = SpritesSessionEnvironments(section, tmp_path, token_reader=vault.get)
    tokens: list[str] = []

    def client_for(token, **_kwargs):
        tokens.append(token)
        return client

    async def found_while_b_is_saved(_name):
        vault["SPRITES_TOKEN"] = "org-b-token"  # lands mid-request
        return {"id": "sprite-id"}

    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        delete_sprite=AsyncMock(),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", side_effect=client_for):
        row = await environments._create("session", profile, "session")
        environments.registry.set_deletion("session", stamp(NOW))
        client.get_sprite.side_effect = found_while_b_is_saved
        assert await environments.sweep_due(now=NOW) == 0
    client.delete_sprite.assert_not_awaited()
    assert environments.registry.get("session").name == row.name
    assert tokens[-1] == "org-b-token"  # the refused DELETE would have used B's


@pytest.mark.asyncio
async def test_a_row_recovered_from_an_interrupted_create_is_pinned_to_its_vm(
    tmp_path, monkeypatch
):
    """Created, but the gateway died before the id was saved: the reopen pins it."""
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "SPRITES_TOKEN", "workdir": ""}
    section = SimpleNamespace(sprites_profiles=[profile])
    environments = SpritesSessionEnvironments(
        section, tmp_path, token_reader=lambda _name: "a-token"
    )
    # The row as the interrupted create left it: a name, no provider id.
    environments.registry.put(SessionResource("session", "fly", "kirocrew-0123456789abcdef", ""))
    client = SimpleNamespace(
        create_sprite=AsyncMock(),
        get_sprite=AsyncMock(return_value={"id": "first-id"}),
        delete_sprite=AsyncMock(),
        # A blank working folder: the start reads the guest's login home.
        exec=AsyncMock(return_value=(0, b"/guest", b"")),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        await environments.prepare("session", "fly")
        assert environments.registry.get("session").resource_id == "first-id"
        # A VM someone made later under the same name is not this session's.
        client.get_sprite.return_value = {"id": "replacement-id"}
        with pytest.raises(SessionEnvironmentUnavailable, match="another machine"):
            await environments.prepare("session", "fly")
        environments.registry.set_deletion("session", stamp(NOW))
        assert await environments.sweep_due(now=NOW) == 0
    client.delete_sprite.assert_not_awaited()
    client.create_sprite.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_token_rotated_in_the_same_account_keeps_the_sprite(tmp_path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(persistent_vm, "report_start_stage", lambda *_: None)
    profile = {"id": "fly", "token_secret": "SPRITES_TOKEN", "workdir": ""}
    section = SimpleNamespace(sprites_profiles=[profile])
    vault = {"SPRITES_TOKEN": "first-token"}
    environments = SpritesSessionEnvironments(section, tmp_path, token_reader=vault.get)
    client = SimpleNamespace(
        create_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        get_sprite=AsyncMock(return_value={"id": "sprite-id"}),
        delete_sprite=AsyncMock(),
        # A blank working folder: the start reads the guest's login home.
        exec=AsyncMock(return_value=(0, b"/guest", b"")),
    )
    with patch("kiro_crew.sprites_session.launch.SpritesClient", return_value=client):
        row = await environments._create("session", profile, "session")
        vault["SPRITES_TOKEN"] = "rotated-token"
        prepared = await environments.prepare("session", "fly")
    assert prepared.created is False
    updated = environments.registry.get("session")
    assert updated.name == row.name
    assert updated.credential_id and updated.credential_id != row.credential_id


@pytest.mark.asyncio
async def test_a_removed_boxd_profile_still_idles_its_archived_machine(tmp_path, monkeypatch):
    """boxd needs no stored credential, so archive still hibernates and arms delete-after."""
    section = SimpleNamespace(boxd_profiles=[])
    environments = BoxdSessionEnvironments(section, tmp_path)
    row = SessionResource("session", "box", NAME, "owned-id")
    environments.registry.put(row)
    cli = SimpleNamespace(
        identity=AsyncMock(return_value="acct"),
        get=AsyncMock(return_value={"id": "owned-id"}),
        hibernate=AsyncMock(),
        set_delete_after=AsyncMock(),
    )
    monkeypatch.setattr(BoxdSessionEnvironments, "cli", AsyncMock(return_value=cli))
    assert await environments.archive_session("session") is True
    cli.hibernate.assert_awaited_once_with(NAME)
    cli.set_delete_after.assert_awaited_once()
    assert environments.registry.get("session").deletion_due_at


def test_a_registry_row_with_a_newer_field_still_loads(tmp_path):
    import json

    environments = BoxdSessionEnvironments(SimpleNamespace(boxd_profiles=[]), tmp_path)
    row = SessionResource("session", "box", NAME, "owned-id")
    environments.registry.put(row)
    path = environments.registry.path
    data = json.loads(path.read_text())
    data["resources"]["session"]["added_later"] = "x"
    path.write_text(json.dumps(data))
    assert environments.registry.get("session") == row
    del data["resources"]["session"]["name"]
    path.write_text(json.dumps(data))
    with pytest.raises(SessionEnvironmentUnavailable, match="unreadable"):
        environments.registry.get("session")


@pytest.mark.asyncio
async def test_removed_boxd_profile_still_allows_owned_machine_deletion(tmp_path):
    cli = SimpleNamespace(
        identity=AsyncMock(return_value="acct"),
        get=AsyncMock(return_value={"id": "owned-id"}),
        remove=AsyncMock(),
    )
    environments = BoxdSessionEnvironments(SimpleNamespace(boxd_profiles=[]), tmp_path, cli=cli)
    row = SessionResource("session", "removed", NAME, "owned-id", deletion_due_at=stamp(NOW))
    environments.registry.put(row)
    assert await environments.sweep_due(now=NOW) == 1
    cli.get.assert_awaited_once_with(NAME)
    cli.remove.assert_awaited_once_with(NAME)
    assert environments.registry.get(row.session_key) is None


@pytest.mark.asyncio
async def test_delete_warnings_follow_row_message_and_provider_across_instances(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(persistent_vm, "_last_delete_error", {})
    caplog.set_level("WARNING", logger=persistent_vm.__name__)
    cli = SimpleNamespace(
        identity=AsyncMock(return_value="acct"),
        get=AsyncMock(return_value={"id": "owned-id"}),
        remove=AsyncMock(side_effect=SessionEnvironmentUnavailable("offline")),
    )
    section = SimpleNamespace(boxd_profiles=[{"id": "box"}])
    row = SessionResource("first", "box", NAME, "owned-id", deletion_due_at=stamp(NOW))
    environments = BoxdSessionEnvironments(section, tmp_path, cli=cli)
    environments.registry.put(row)

    def warnings():
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == persistent_vm.__name__
        ]

    for _ in range(3):
        environments = BoxdSessionEnvironments(section, tmp_path, cli=cli)
        assert await environments.sweep_due(now=NOW) == 0
    assert cli.remove.await_count == 3, "a suppressed warning must not suppress the retry"
    assert len(warnings()) == 1
    assert NAME in warnings()[0] and "offline" in warnings()[0]
    cli.remove.side_effect = SessionEnvironmentUnavailable("signed out")
    assert await environments.sweep_due(now=NOW) == 0
    assert len(warnings()) == 2 and "signed out" in warnings()[-1]
    second = SessionResource(
        "second", "box", "kc-fedcba9876543210", "owned-id", deletion_due_at=stamp(NOW)
    )
    environments.registry.put(second)
    assert await environments.sweep_due(now=NOW) == 0
    assert len(warnings()) == 3 and second.name in warnings()[-1]
    # Provider labels keep identical row names independent.
    environments.label = "Sprites"
    assert await environments.sweep_due(now=NOW) == 0
    assert len(warnings()) == 5
    environments.label = "boxd"
    cli.remove.side_effect = None
    assert await environments.sweep_due(now=NOW) == 2
    assert not any(label == "boxd" for label, _ in persistent_vm._last_delete_error)
    # A recovered row can warn again if a new deletion attempt fails.
    environments.registry.put(row)
    cli.remove.side_effect = SessionEnvironmentUnavailable("signed out")
    assert await environments.sweep_due(now=NOW) == 0
    assert len(warnings()) == 6
    environments.registry.set_deletion(row.session_key, "")
    assert await environments.sweep_due(now=NOW) == 0
    assert not any(label == "boxd" for label, _ in persistent_vm._last_delete_error)
