"""Security contract tests for dashboard tag provenance and policy projection."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard import chat_tag_grants
from kiro_crew.dashboard import chat_tags as chat_tags_module
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY


def _request_identity(*, user: str = "local-app", app_name: str = "", member: str = ""):
    @web.middleware
    async def _middleware(request, handler):
        request["user"] = user
        request["app"] = app_name
        if member:
            request[MEMBER_CHAT_PRINCIPAL_KEY] = member
        return await handler(request)

    return _middleware


@pytest.fixture(autouse=True)
def _isolated_grant_store(tmp_path, monkeypatch):
    """Keep the protected policy store and signing chain inside the test home."""
    from kiro_crew.dashboard import token_secret

    monkeypatch.setattr(chat_tag_grants, "config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_secret, "_get_secret", lambda: b"test-signing-key")
    chat_tag_grants._cache = None
    chat_tag_grants._degraded = None
    chat_tag_grants._quarantined_this_boot = False
    chat_tag_grants._quarantine_repaired = False
    yield
    chat_tag_grants._cache = None
    chat_tag_grants._degraded = None
    chat_tag_grants._quarantined_this_boot = False
    chat_tag_grants._quarantine_repaired = False


class TestTagProvenancePolicy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected"),
        [(False, ("none", False)), (True, ("add-remove", True))],
    )
    async def test_dashboard_create_records_protected_identity(self, tmp_path, status, expected):
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/tags", json={"name": "Created label", "status": status}
            )
            tag = await response.json()

        assert response.status == 201
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.has_grant_row(tag["id"])
        assert chat_tag_grants.resolve_grant(tag["id"]) == expected

    @pytest.mark.asyncio
    async def test_internal_mcp_create_stays_rowless_until_owner_adoption(self, tmp_path):
        """A verified dashboard-MCP session may add vocabulary, not its own grant."""
        state = _make_state(tmp_path)
        caller = _ChatSlot(key="caller")
        state._slots[caller.key] = caller
        app = _make_tags_app(state, booted_store=False)
        headers = {
            "X-Internal-Secret": "s3cret",
            "X-Internal-Caller": "kirocrew-dashboard",
            "X-Session-Key": "dashboard:caller",
        }

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/tags",
                json={"name": "Agent-created state", "status": True},
                headers=headers,
            )
            tag = await response.json()
            listed = await (await client.get("/api/chat/tags", headers=headers)).json()

        assert response.status == 201
        assert any(row.get("id") == tag["id"] for row in state._tags)
        projected = next(row for row in listed if row["id"] == tag["id"])
        assert projected["agent_provenanced"] is False
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag["id"])
        assert chat_tag_grants.resolve_grant(tag["id"]) == ("none", False)

        result = await apply_session_directive(
            state,
            caller,
            "dashboard:caller",
            "chat_tag",
            {"set_state": tag["id"]},
            producer_is_user_facing=True,
        )
        assert result == f"Error: tag_grants_unavailable:{tag['id']}"
        assert caller.tags == []

    @pytest.mark.asyncio
    async def test_planted_hex_id_cannot_acquire_authority(self, tmp_path):
        planted_id = "012345abcdef"
        planted = {
            "id": planted_id,
            "name": "Planted label",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(planted)]
        state.save_tags_snapshot([dict(planted)])
        assert chat_tag_grants.seed_default_grants([])
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            status_response = await client.patch(
                f"/api/chat/tags/{planted_id}", json={"status": True}
            )
            status_body = await status_response.json()
            policy_response = await client.patch(
                f"/api/chat/tags/{planted_id}",
                json={"agent": "add-remove", "status": False},
            )
            policy_body = await policy_response.json()

        assert status_response.status == 400
        assert status_body["code"] == "tag_id_not_grantable"
        assert policy_response.status == 400
        assert policy_body["code"] == "tag_id_not_grantable"
        # The tag manager shows this reason verbatim, so it must name the
        # adoption control by the label the UI actually renders.
        locale = json.loads(
            (
                Path(__file__).resolve().parent.parent / "website/src/i18n/locales/en.manual.json"
            ).read_text(encoding="utf-8")
        )
        adopt_label = locale["components"]["tagManagerList"]["adopt_tag"]
        assert adopt_label in status_body["error"]
        assert adopt_label in policy_body["error"]
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(planted_id)
        assert chat_tag_grants.resolve_grant(planted_id) == ("none", False)
        assert state._tags == [planted]

    @pytest.mark.asyncio
    async def test_legacy_custom_tag_stays_ungrantable_until_adopted(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            created = await (await client.post("/api/chat/tags", json={"name": "Legacy"})).json()
            chat_tag_grants.revoke_grant(created["id"])
            chat_tag_grants.refresh_cache()

            policy_response = await client.patch(
                f"/api/chat/tags/{created['id']}",
                json={"agent": "add-only", "status": False},
            )
            policy_body = await policy_response.json()
            rename_response = await client.patch(
                f"/api/chat/tags/{created['id']}", json={"name": "Legacy renamed"}
            )

        assert policy_response.status == 400
        assert policy_body["code"] == "tag_id_not_grantable"
        assert rename_response.status == 200
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(created["id"])

    @pytest.mark.asyncio
    async def test_owner_adopts_legacy_identity_before_policy_patch(self, tmp_path):
        tag_id = "555555555555"
        tag = {
            "id": tag_id,
            "name": "Legacy",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": False})
            adopted_body = await adopted.json()
            updated = await client.patch(f"/api/chat/tags/{tag_id}", json={"agent": "add-only"})

        assert adopted.status == 200
        assert adopted_body["agent"] == "none"
        assert adopted_body["agent_provenanced"] is True
        assert updated.status == 200
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.resolve_grant(tag_id) == ("add-only", False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"status": "false"}, 400, "invalid_status"),
            ({"status": True}, 409, "tag_changed"),
            ({"status": False, "agent": "add-only"}, 400, "invalid_adoption"),
        ],
    )
    async def test_adoption_rejects_malformed_or_stale_intent(self, tmp_path, body, status, code):
        tag_id = "666666666666"
        tag = {
            "id": tag_id,
            "name": "Legacy",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/chat/tags/{tag_id}/adopt", json=body)
            response_body = await response.json()

        assert response.status == status
        assert response_body["code"] == code
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["unattributable", "app", "member", "internal", "forged"])
    async def test_adoption_is_dashboard_owner_only(self, tmp_path, caller):
        tag_id = "777777777777"
        tag = {
            "id": tag_id,
            "name": "Legacy",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        app = _make_tags_app(state, authenticate_owner=caller not in {"unattributable", "forged"})
        headers: dict[str, str] = {}
        body: dict[str, object] = {"status": False}
        if caller == "app":
            app.middlewares.append(_request_identity(app_name="example-app"))
        elif caller == "member":
            app.middlewares.append(_request_identity(member="member:store-1"))
        elif caller == "internal":
            app.middlewares.append(_request_identity())
            headers = {
                "X-Internal-Secret": "s3cret",
                "X-Internal-Caller": "kirocrew-dashboard",
            }
        elif caller == "forged":
            body.update({"user": "local-app", "app": ""})

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                f"/api/chat/tags/{tag_id}/adopt", json=body, headers=headers
            )
            response_body = await response.json()

        assert response.status == 403
        assert response_body["code"] == "owner_required"
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)

    @pytest.mark.asyncio
    async def test_adoption_fails_closed_on_degraded_store(self, tmp_path):
        tag_id = "888888888888"
        tag = {
            "id": tag_id,
            "name": "Legacy",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": False})
            body = await response.json()

        assert response.status == 500
        assert body["code"] == "persist_failed"
        assert chat_tag_grants._store_path().read_text(encoding="utf-8") == "{broken"
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)

    @pytest.mark.asyncio
    async def test_adoption_race_never_resets_existing_policy(self, tmp_path):
        tag_id = "999999999999"
        tag = {
            "id": tag_id,
            "name": "Legacy",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        chat_tag_grants.mint_grant(tag_id, policy="add-only", status=False)
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": False})

        assert response.status == 200
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.resolve_grant(tag_id) == ("add-only", False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("store_state", ["missing", "corrupt"])
    async def test_patch_fails_closed_when_provenance_is_unavailable(self, tmp_path, store_state):
        tag_id = "abcdef012345"
        tag = {
            "id": tag_id,
            "name": "Unavailable provenance",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        if store_state == "corrupt":
            store_path = chat_tag_grants._store_path()
            store_path.parent.mkdir(parents=True)
            store_path.write_text("{broken", encoding="utf-8")
        app = _make_tags_app(state, booted_store=False)

        async with TestClient(TestServer(app)) as client:
            response = await client.patch(f"/api/chat/tags/{tag_id}", json={"status": True})
            body = await response.json()

        assert response.status == 500
        assert body["code"] == "persist_failed"
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)
        assert state._tags == [tag]

    @pytest.mark.asyncio
    async def test_get_projects_policy_without_persisting_derived_fields(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            created = await (await client.post("/api/chat/tags", json={"name": "Label"})).json()
            listed = await (await client.get("/api/chat/tags")).json()

            projected = next(row for row in listed if row["id"] == created["id"])
            assert projected["agent"] == "none"
            assert projected["agent_provenanced"] is True
            assert projected["agent_store_degraded"] is False

            chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
            degraded = await (await client.get("/api/chat/tags")).json()

        degraded_row = next(row for row in degraded if row["id"] == created["id"])
        assert degraded_row["agent"] == "none"
        assert degraded_row["agent_provenanced"] is False
        assert degraded_row["agent_store_degraded"] is True

        persisted = json.loads((tmp_path / "tags.json").read_text(encoding="utf-8"))
        persisted_row = next(row for row in persisted if row["id"] == created["id"])
        assert "agent" not in persisted_row
        assert "agent_provenanced" not in persisted_row
        assert "agent_store_degraded" not in persisted_row

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [False, True])
    async def test_create_reconciles_a_commit_reported_as_failure(
        self, tmp_path, monkeypatch, status
    ):
        tag_id = "111111111111" if status else "222222222222"
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        real_write = chat_tags_module._write_tags_snapshot

        def _committed_then_failed_write(write_state, snapshot):
            real_write(write_state, snapshot)
            raise OSError("simulated late vocabulary failure")

        monkeypatch.setattr(chat_tags_module.uuid, "uuid4", lambda: MagicMock(hex=tag_id))
        monkeypatch.setattr(chat_tags_module, "_write_tags_snapshot", _committed_then_failed_write)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/tags", json={"name": "Ambiguous commit", "status": status}
            )
            committed = await response.json()

        assert response.status == 201
        assert committed["id"] == tag_id
        assert state._tags == [committed]
        chat_tag_grants.refresh_cache()
        expected = ("add-remove", True) if status else ("none", False)
        assert chat_tag_grants.has_grant_row(tag_id)
        assert chat_tag_grants.resolve_grant(tag_id) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("existing_snapshot", [False, True])
    async def test_create_confirmed_absent_removes_memory_and_identity(
        self, tmp_path, monkeypatch, existing_snapshot
    ):
        tag_id = "333333333333"
        existing = {
            "id": "existing",
            "name": "Existing",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        }
        state = _make_state(tmp_path)
        if existing_snapshot:
            state._tags = [dict(existing)]
            state.save_tags_snapshot([dict(existing)])
        app = _make_tags_app(state)

        def _failed_before_commit(_write_state, _snapshot):
            raise OSError("simulated pre-commit vocabulary failure")

        monkeypatch.setattr(chat_tags_module.uuid, "uuid4", lambda: MagicMock(hex=tag_id))
        monkeypatch.setattr(chat_tags_module, "_write_tags_snapshot", _failed_before_commit)

        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/chat/tags", json={"name": "Absent"})

        assert response.status == 500
        assert state._tags == ([existing] if existing_snapshot else [])
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)

    @pytest.mark.asyncio
    async def test_create_unreadable_reconciliation_withdraws_memory_and_identity(
        self, tmp_path, monkeypatch
    ):
        tag_id = "444444444444"
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        (tmp_path / "tags.json").mkdir()

        def _ambiguous_write(_write_state, _snapshot):
            raise OSError("simulated ambiguous vocabulary failure")

        monkeypatch.setattr(chat_tags_module.uuid, "uuid4", lambda: MagicMock(hex=tag_id))
        monkeypatch.setattr(chat_tags_module, "_write_tags_snapshot", _ambiguous_write)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/tags", json={"name": "Withdrawn", "status": True}
            )
            listed = await (await client.get("/api/chat/tags")).json()

        assert response.status == 500
        assert state._tags == []
        assert all(tag["id"] != tag_id for tag in listed)
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)
        assert chat_tag_grants.resolve_grant(tag_id) == ("none", False)

    @pytest.mark.asyncio
    async def test_late_patch_mint_error_keeps_the_committed_grant(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        real_mint = chat_tags_module.mint_grant

        def _committed_then_failed_mint(tag_id, *, policy, status):
            real_mint(tag_id, policy=policy, status=status)
            if policy == "add-only":
                raise OSError("simulated late grant failure")

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Narrow", "status": True})
            ).json()
            monkeypatch.setattr(chat_tags_module, "mint_grant", _committed_then_failed_mint)
            response = await client.patch(f"/api/chat/tags/{tag['id']}", json={"agent": "add-only"})

        assert response.status == 200
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.resolve_grant(tag["id"]) == ("add-only", True)


_LEGACY_TAG_ID = "aaaaaaaaaaaa"
_DEFAULT_STATUS_ID = "review"


def _quarantine_vocabulary() -> list[dict]:
    return [
        {
            "id": _LEGACY_TAG_ID,
            "name": "Legacy custom",
            "color": "#6b7280",
            "order": 0,
            "status": False,
        },
        {
            "id": _DEFAULT_STATUS_ID,
            "name": "Review",
            "color": "#6b7280",
            "order": 1,
            "status": True,
        },
    ]


def _break_store(monkeypatch, cause: str) -> None:
    """Make the installed store unverifiable the way a boot pass would find it."""
    from kiro_crew.dashboard import token_secret

    if cause == "stale_signer":
        # A rotated token_signing.key breaks the store key's certificate: a
        # legitimate store and a planted self-signed one are indistinguishable.
        monkeypatch.setattr(token_secret, "_get_secret", lambda: b"rotated-signing-key")
    elif cause == "corrupt_store":
        chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(cause)


def _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, cause: str):
    """A healthy earlier boot minted identity for a custom tag; THIS boot finds
    the store unverifiable, quarantines it and reseeds the trusted defaults."""
    state = _make_state(tmp_path)
    vocab = _quarantine_vocabulary()
    state._tags = [dict(t) for t in vocab]
    state.save_tags_snapshot([dict(t) for t in vocab])
    assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID])
    chat_tag_grants.mint_grant(_LEGACY_TAG_ID, policy="none", status=False)
    chat_tag_grants.refresh_cache()
    assert chat_tag_grants.has_grant_row(_LEGACY_TAG_ID)
    _break_store(monkeypatch, cause)
    return state


class TestQuarantineRecovery:
    """A boot quarantine must never trust the old rows, but once the reseed
    that followed it has been read back and verified, the CURRENT store is a
    healthy one and explicit owner adoption -- the recovery the quarantine
    points at -- must work on the same boot, without a gateway restart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cause", ["stale_signer", "corrupt_store"])
    async def test_owner_adoption_succeeds_same_boot_after_verified_reseed(
        self, tmp_path, monkeypatch, cause
    ):
        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, cause)
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is True
        chat_tag_grants.refresh_cache()
        # The old identity is gone (never trusted), the defaults are back.
        assert not chat_tag_grants.has_grant_row(_LEGACY_TAG_ID)
        assert chat_tag_grants.resolve_grant(_DEFAULT_STATUS_ID) == ("add-remove", True)
        store_dir = chat_tag_grants._store_path().parent
        assert list(store_dir.glob("*.quarantined-*")), "quarantined bytes are kept as evidence"
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(
                f"/api/chat/tags/{_LEGACY_TAG_ID}/adopt", json={"status": False}
            )
            adopted_body = await adopted.json()
            updated = await client.patch(
                f"/api/chat/tags/{_LEGACY_TAG_ID}", json={"agent": "add-only"}
            )
            updated_body = await updated.json()

        assert (adopted.status, adopted_body.get("code")) == (200, None)
        assert adopted_body["agent_provenanced"] is True
        assert (updated.status, updated_body.get("code")) == (200, None)
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.resolve_grant(_LEGACY_TAG_ID) == ("add-only", False)

    @pytest.mark.asyncio
    async def test_provenanced_status_and_policy_writes_succeed_after_reseed(
        self, tmp_path, monkeypatch
    ):
        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "stale_signer")
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is True
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            policy = await client.patch(
                f"/api/chat/tags/{_DEFAULT_STATUS_ID}", json={"agent": "add-only"}
            )
            policy_body = await policy.json()
            created = await client.post("/api/chat/tags", json={"name": "Fresh", "status": True})
            created_body = await created.json()
            status = await client.patch(
                f"/api/chat/tags/{created_body['id']}", json={"status": False}
            )
            status_body = await status.json()

        assert (policy.status, policy_body.get("code")) == (200, None)
        assert created.status == 201
        assert (status.status, status_body.get("code")) == (200, None)
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.resolve_grant(_DEFAULT_STATUS_ID) == ("add-only", True)
        assert chat_tag_grants.resolve_grant(created_body["id"]) == ("none", False)

    @pytest.mark.asyncio
    async def test_rowless_custom_tag_stays_untrusted_until_adopted(self, tmp_path, monkeypatch):
        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "stale_signer")
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is True
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            policy = await client.patch(
                f"/api/chat/tags/{_LEGACY_TAG_ID}", json={"agent": "add-remove"}
            )
            policy_body = await policy.json()
            status = await client.patch(f"/api/chat/tags/{_LEGACY_TAG_ID}", json={"status": True})
            status_body = await status.json()
            listed = await (await client.get("/api/chat/tags")).json()

        # Refused as NOT PROVENANCED (a human decision away), not as a store
        # fault: the store is healthy, the identity simply was never re-minted.
        assert (policy.status, policy_body["code"]) == (400, "tag_id_not_grantable")
        assert (status.status, status_body["code"]) == (400, "tag_id_not_grantable")
        projected = next(row for row in listed if row["id"] == _LEGACY_TAG_ID)
        assert projected["agent"] == "none"
        assert projected["agent_provenanced"] is False
        assert projected["agent_store_degraded"] is False
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(_LEGACY_TAG_ID)
        assert chat_tag_grants.resolve_grant(_LEGACY_TAG_ID) == ("none", False)
        assert state._tags[0]["status"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure", ["reseed_write_fails", "reseed_readback_fails", "quarantine_rename_fails"]
    )
    async def test_unverified_reseed_still_rejects_adoption(self, tmp_path, monkeypatch, failure):
        from pathlib import Path

        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "corrupt_store")
        if failure == "reseed_write_fails":

            def _refuse_write(_path, _document):
                raise OSError("simulated reseed write failure")

            monkeypatch.setattr(chat_tag_grants, "_write_document", _refuse_write)
            assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is False
        elif failure == "reseed_readback_fails":
            # The write reports success, but the bytes that reached disk do not
            # verify: the installed snapshot must not be taken on faith.
            real_atomic_write = chat_tag_grants.atomic_write

            def _tampered_atomic_write(path, content, **kwargs):
                if Path(path).name == chat_tag_grants._STORE_FILENAME:
                    content = content.replace('"provenance"', '"provenanze"')
                real_atomic_write(path, content, **kwargs)

            monkeypatch.setattr(chat_tag_grants, "atomic_write", _tampered_atomic_write)
            chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID])
        else:

            def _refuse_rename(self, _target):
                raise OSError("simulated rename failure")

            monkeypatch.setattr(Path, "rename", _refuse_rename)
            assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is False
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(
                f"/api/chat/tags/{_LEGACY_TAG_ID}/adopt", json={"status": False}
            )
            adopted_body = await adopted.json()
            updated = await client.patch(
                f"/api/chat/tags/{_DEFAULT_STATUS_ID}", json={"agent": "add-only"}
            )
            updated_body = await updated.json()

        assert (adopted.status, adopted_body["code"]) == (500, "persist_failed")
        assert (updated.status, updated_body["code"]) == (500, "persist_failed")
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(_LEGACY_TAG_ID)
        assert chat_tag_grants.resolve_grant(_DEFAULT_STATUS_ID) in (
            ("none", False),
            ("add-remove", True),
        )

    @pytest.mark.asyncio
    async def test_owner_create_refuses_to_recreate_a_store_missing_after_failed_reseed(
        self, tmp_path, monkeypatch
    ):
        """The quarantine moved the broken store aside and the reseed write
        failed, so NO store file exists. ``mint_grant`` would recreate one from
        an empty document -- an owner row in a store without the trusted
        defaults. The create must hit the same write gate as PATCH/adoption."""
        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "corrupt_store")

        def _refuse_write(_path, _document):
            raise OSError("simulated reseed write failure")

        real_write_document = chat_tag_grants._write_document
        monkeypatch.setattr(chat_tag_grants, "_write_document", _refuse_write)
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is False
        # Only the reseed failed: the owner's later mint gets the real writer.
        monkeypatch.setattr(chat_tag_grants, "_write_document", real_write_document)
        assert not chat_tag_grants._store_path().exists()
        tags_before = [dict(t) for t in state._tags]
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/chat/tags", json={"name": "Fresh", "status": True})
            created_body = await created.json()

        assert (created.status, created_body.get("code")) == (500, "persist_failed")
        assert not chat_tag_grants._store_path().exists(), "no store rebuilt without defaults"
        assert state._tags == tags_before

    @pytest.mark.asyncio
    async def test_owner_create_refused_by_the_write_gate_is_audited(self, tmp_path, monkeypatch):
        """The write-gate refusal is a permission decision: it lands on the SEL
        as ``chat.tag_create`` ``denied`` naming the store condition, like the
        sibling PATCH and adoption refusals."""
        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "stale_signer")
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is True
        chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
        audit = MagicMock()
        monkeypatch.setattr(chat_tags_module, "sel", lambda: audit)
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/chat/tags", json={"name": "Fresh", "status": True})
            created_body = await created.json()

        assert (created.status, created_body.get("code")) == (500, "persist_failed")
        creates = [
            call.kwargs
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("operation") == "chat.tag_create"
        ]
        assert [(c["outcome"], c["error"]) for c in creates] == [
            ("denied", "grant store unreadable")
        ]

    @pytest.mark.asyncio
    async def test_store_broken_after_verified_reseed_rejects_again(self, tmp_path, monkeypatch):
        state = _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "stale_signer")
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is True
        chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())

        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(
                f"/api/chat/tags/{_LEGACY_TAG_ID}/adopt", json={"status": False}
            )
            adopted_body = await adopted.json()

        assert (adopted.status, adopted_body["code"]) == (500, "persist_failed")
        assert chat_tag_grants._store_path().read_text(encoding="utf-8") == "{broken"

    def test_write_gate_and_reduced_grants_signal_are_distinct(self, tmp_path, monkeypatch):
        """``store_degraded`` keeps naming the quarantine for the whole boot (a
        rowless refusal stays a store condition for the applier); the write
        gate reopens once the reseed is verified and closes again on a broken
        or missing current store."""
        _boot_with_prior_identity_then_quarantine(tmp_path, monkeypatch, "stale_signer")
        assert chat_tag_grants.seed_default_grants([_DEFAULT_STATUS_ID]) is True
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.store_degraded() == "quarantined"
        assert chat_tag_grants.store_write_blocked() is None
        chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.store_write_blocked() == "unreadable"
        chat_tag_grants._store_path().unlink()
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.store_write_blocked() == "missing"
        assert chat_tag_grants.store_degraded() == "quarantined"


class TestRefusedPatchIsAudited:
    """A refused status/policy PATCH is a permission decision and lands on the SEL."""

    @staticmethod
    def _denials(audit: MagicMock) -> list[dict]:
        return [
            call.kwargs
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("operation") == "chat.tag_update"
            and call.kwargs.get("outcome") == "denied"
        ]

    @pytest.mark.asyncio
    async def test_rowless_tag_refusal_is_audited(self, tmp_path, monkeypatch):
        tag_id = "777777777777"
        tag = {"id": tag_id, "name": "Legacy", "color": "#6b7280", "order": 0, "status": False}
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        audit = MagicMock()
        monkeypatch.setattr(chat_tags_module, "sel", lambda: audit)
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            response = await client.patch(f"/api/chat/tags/{tag_id}", json={"agent": "add-only"})
            body = await response.json()

        assert (response.status, body["code"]) == (400, "tag_id_not_grantable")
        denials = self._denials(audit)
        assert len(denials) == 1
        assert denials[0]["resources"] == tag_id
        assert denials[0]["caller"] == "dashboard"
        assert denials[0]["error"] == "tag has no protected identity"

    @pytest.mark.asyncio
    async def test_degraded_store_refusal_is_audited(self, tmp_path, monkeypatch):
        tag_id = "666666666666"
        tag = {"id": tag_id, "name": "Legacy", "color": "#6b7280", "order": 0, "status": False}
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        chat_tag_grants._store_path().write_text("{broken", encoding="utf-8")
        audit = MagicMock()
        monkeypatch.setattr(chat_tags_module, "sel", lambda: audit)
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            response = await client.patch(f"/api/chat/tags/{tag_id}", json={"status": True})
            body = await response.json()

        assert (response.status, body["code"]) == (500, "persist_failed")
        denials = self._denials(audit)
        assert len(denials) == 1
        assert denials[0]["resources"] == tag_id
        assert denials[0]["error"].startswith("grant store ")


class TestAdoptionAuditAndCorrection:
    """Every adoption outcome past the owner check lands on the SEL, and a legacy
    non-boolean status is corrected by the owner's explicit boolean."""

    @staticmethod
    def _adopt_events(audit: MagicMock, outcome: str) -> list[dict]:
        return [
            call.kwargs
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("operation") == "chat.tag_adopt"
            and call.kwargs.get("outcome") == outcome
        ]

    @staticmethod
    def _app(tmp_path, monkeypatch, tag):
        state = _make_state(tmp_path)
        state._tags = [dict(tag)]
        state.save_tags_snapshot([dict(tag)])
        assert chat_tag_grants.seed_default_grants([])
        audit = MagicMock()
        monkeypatch.setattr(chat_tags_module, "sel", lambda: audit)
        app = _make_tags_app(state)
        app.middlewares.append(_request_identity())
        return state, app, audit

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tag_id", "stored_status", "body", "code", "reason"),
        [
            (
                "legacy-slug",
                False,
                {"status": False},
                "tag_id_not_grantable",
                "tag id is not grantable",
            ),
            (
                "444444444444",
                False,
                {"status": True},
                "tag_changed",
                "tag status changed since it was shown",
            ),
            (
                "444444444444",
                False,
                {"status": "false"},
                "invalid_status",
                "status is not a boolean",
            ),
        ],
    )
    async def test_refused_adoption_is_audited(
        self, tmp_path, monkeypatch, tag_id, stored_status, body, code, reason
    ):
        tag = {
            "id": tag_id,
            "name": "Legacy",
            "color": "#6b7280",
            "order": 0,
            "status": stored_status,
        }
        _state, app, audit = self._app(tmp_path, monkeypatch, tag)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/chat/tags/{tag_id}/adopt", json=body)
            response_body = await response.json()

        assert response_body["code"] == code
        denials = self._adopt_events(audit, "denied")
        assert [(d["resources"], d["error"]) for d in denials] == [(tag_id, reason)]
        assert self._adopt_events(audit, "allowed") == []

    @pytest.mark.asyncio
    async def test_adoption_normalizes_a_non_boolean_legacy_status(self, tmp_path, monkeypatch):
        tag_id = "333333333333"
        tag = {"id": tag_id, "name": "Legacy", "color": "#6b7280", "order": 0, "status": "true"}
        state, app, audit = self._app(tmp_path, monkeypatch, tag)

        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": True})
            adopted_body = await adopted.json()
            updated = await client.patch(f"/api/chat/tags/{tag_id}", json={"agent": "add-only"})

        assert adopted.status == 200
        assert adopted_body["status"] is True
        assert adopted_body["agent_provenanced"] is True
        durable = state.read_durable_tags_snapshot()
        assert durable is not None
        assert [t["status"] for t in durable.tags if t["id"] == tag_id] == [True]
        assert updated.status == 200
        chat_tag_grants.refresh_cache()
        assert chat_tag_grants.resolve_grant(tag_id) == ("add-only", True)
        assert len(self._adopt_events(audit, "allowed")) == 1

    @pytest.mark.asyncio
    async def test_failed_status_normalization_leaves_the_tag_rowless(self, tmp_path, monkeypatch):
        """A 500 adoption is not a commit: when the non-boolean status cannot be
        written, no identity row exists, so the tag stays rowless and the owner
        can retry adoption. The status write precedes the mint, so there is no
        minted row for a failing compensation to strand."""
        tag_id = "444444444444"
        tag = {"id": tag_id, "name": "Legacy", "color": "#6b7280", "order": 0, "status": "true"}
        _state, app, audit = self._app(tmp_path, monkeypatch, tag)

        def _refuse(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(chat_tags_module, "_write_tags_snapshot", _refuse)
        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": True})
            adopted_body = await adopted.json()
            listed = await client.get("/api/chat/tags")
            rows = await listed.json()

        assert (adopted.status, adopted_body["code"]) == (500, "persist_failed")
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)
        assert [r["agent_provenanced"] for r in rows if r["id"] == tag_id] == [False]
        assert [e["error"] for e in self._adopt_events(audit, "error")] == [
            "status normalization failed"
        ]

    @pytest.mark.asyncio
    async def test_normalization_is_written_before_the_identity_mint(self, tmp_path, monkeypatch):
        """A failed normalization never calls ``mint_grant``: the storage
        failure GPT described (write and compensation both failing) has no
        minted row to leave behind."""
        tag_id = "555555555555"
        tag = {"id": tag_id, "name": "Legacy", "color": "#6b7280", "order": 0, "status": "true"}
        _state, app, _audit = self._app(tmp_path, monkeypatch, tag)
        mints: list[str] = []
        real_mint = chat_tags_module.mint_grant

        def _counting_mint(grant_tid, **kwargs):
            mints.append(grant_tid)
            return real_mint(grant_tid, **kwargs)

        def _refuse(*_args, **_kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(chat_tags_module, "mint_grant", _counting_mint)
        monkeypatch.setattr(chat_tags_module, "_write_tags_snapshot", _refuse)
        monkeypatch.setattr(chat_tags_module, "revoke_grant", _refuse)
        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": True})

        assert adopted.status == 500
        assert mints == []
        chat_tag_grants.refresh_cache()
        assert not chat_tag_grants.has_grant_row(tag_id)

    @pytest.mark.asyncio
    async def test_failed_grant_write_on_patch_is_audited(self, tmp_path, monkeypatch):
        tag_id = "222222222222"
        tag = {"id": tag_id, "name": "Legacy", "color": "#6b7280", "order": 0, "status": False}
        _state, app, audit = self._app(tmp_path, monkeypatch, tag)

        def _refuse(*_args, **_kwargs):
            raise OSError("disk full")

        async with TestClient(TestServer(app)) as client:
            adopted = await client.post(f"/api/chat/tags/{tag_id}/adopt", json={"status": False})
            assert adopted.status == 200
            monkeypatch.setattr(chat_tags_module, "mint_grant", _refuse)
            response = await client.patch(f"/api/chat/tags/{tag_id}", json={"agent": "add-only"})
            body = await response.json()

        assert (response.status, body["code"]) == (500, "persist_failed")
        errors = [
            call.kwargs
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("operation") == "chat.tag_update"
            and call.kwargs.get("outcome") == "error"
        ]
        assert [(e["resources"], e["error"]) for e in errors] == [
            (tag_id, "grant downgrade failed")
        ]
