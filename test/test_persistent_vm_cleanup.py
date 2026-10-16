"""Shutdown and retention only act on owned persistent machines."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
from kiro_crew.coder import persistent_vm, retention
from kiro_crew.coder.persistent_vm import SessionResource, stamp
from kiro_crew.config import paths
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

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
        get=AsyncMock(return_value={"id": live_id} if live_id else None), hibernate=AsyncMock()
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
async def test_shutdown_continues_after_one_boxd_identity_lookup_fails(tmp_path, monkeypatch):
    section = SimpleNamespace(boxd_profiles=[{"id": "box"}])
    environments = BoxdSessionEnvironments(section, tmp_path)
    first = SessionResource("first", "box", NAME, "id-first")
    second = SessionResource("second", "box", "kc-fedcba9876543210", "id-second")
    for row in (first, second):
        environments.registry.put(row)
    cli = SimpleNamespace(
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
async def test_removed_boxd_profile_still_allows_owned_machine_deletion(tmp_path):
    cli = SimpleNamespace(get=AsyncMock(return_value={"id": "owned-id"}), remove=AsyncMock())
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
