"""Tests for kiro_crew.dashboard.handlers.secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.secrets import SecretVault


class _FakeState:
    """No owner configured: only the signed local bootstrap subjects pass."""

    owner_id = ""


def _app() -> web.Application:
    """A dashboard app whose secrets routes see an AUTHENTICATED owner caller.

    Stands in for ``token_auth_middleware``: the secrets handlers gate on
    ``is_owner_dashboard_request``, which reads ``request["user"]`` /
    ``request["app"]`` and ``app["state"].owner_id``. With no owner configured,
    the default local-app subject (empty app + ``local-app`` user) is the
    implicit owner, so existing behavioural tests exercise the authorized path.
    A test can select a different caller via the ``X-Test-User`` /
    ``X-Test-App`` headers.
    """

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    setup_secrets_routes(app)
    return app


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """Create a vault with test data."""
    vault = SecretVault(tmp_path)
    vault._set_sync("TEST_KEY", "test-value-123")
    vault._set_sync("DB_PASS", "hunter2")
    return tmp_path


@pytest.fixture()
def empty_vault_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestApiSecretsList:
    """Tests for GET /api/secrets."""

    @pytest.mark.asyncio
    async def test_lists_names_sorted(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": ["DB_PASS", "TEST_KEY"]}

    @pytest.mark.asyncio
    async def test_empty_vault(self, empty_vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(empty_vault_dir)
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": []}


class TestApiSecretsSet:
    """Tests for POST /api/secrets."""

    @pytest.mark.asyncio
    async def test_stores_secret(self, tmp_path: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    json={"name": "NEW_KEY", "value": "new-value"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                # Verify stored
                vault = SecretVault(tmp_path)
                assert vault.get("NEW_KEY").reveal() == "new-value"

    @pytest.mark.asyncio
    async def test_missing_name(self, tmp_path: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"value": "x"})
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_value(self, tmp_path: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "X"})
                assert resp.status == 400


class TestApiSecretsDelete:
    """Tests for DELETE /api/secrets/{name}."""

    @pytest.mark.asyncio
    async def test_deletes_secret(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/TEST_KEY")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                vault = SecretVault(vault_dir)
                assert vault.get("TEST_KEY") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/MISSING")
                assert resp.status == 200  # delete is idempotent


class TestApiSecretsOwnerAuthorization:
    """Every /api/secrets route is owner-only (CWE-862).

    The AES-256-GCM vault is machine-global keystone-floor material, so an app
    token or any authenticated non-owner dashboard subject must not enumerate,
    overwrite/poison, or delete entries. The handlers gate on
    ``is_owner_dashboard_request`` — an app-scoped caller (non-empty ``app``)
    and a non-owner user are both refused with 403 ``owner_only``, and nothing
    is written.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path", "json_body"),
        [
            ("get", "/api/secrets", None),
            ("post", "/api/secrets", {"name": "EVIL", "value": "x"}),
            ("delete", "/api/secrets/TEST_KEY", None),
        ],
    )
    async def test_app_token_is_refused(
        self, vault_dir: Path, method: str, path: str, json_body: object
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                # X-Test-App non-empty => an app-scoped (non-owner) caller.
                headers = {"X-Test-App": "some-app", "X-Test-User": "some-app-subject"}
                resp = await getattr(client, method)(path, headers=headers, json=json_body)
                assert resp.status == 403
                data = await resp.json()
                assert data["code"] == "owner_only"
                # The write surfaces must not have mutated the vault.
                assert sorted(SecretVault(vault_dir).list_names()) == ["DB_PASS", "TEST_KEY"]

    @pytest.mark.asyncio
    async def test_non_owner_user_is_refused_when_owner_configured(self, vault_dir: Path) -> None:
        app = _app()
        app["state"].owner_id = "U_OWNER"  # type: ignore[attr-defined]

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                headers = {"X-Test-User": "U_SOMEONE_ELSE"}
                resp = await client.post(
                    "/api/secrets", headers=headers, json={"name": "EVIL", "value": "x"}
                )
                assert resp.status == 403
                data = await resp.json()
                assert data["code"] == "owner_only"
                assert sorted(SecretVault(vault_dir).list_names()) == ["DB_PASS", "TEST_KEY"]

    @pytest.mark.asyncio
    async def test_denial_is_audited(self, vault_dir: Path) -> None:
        """A non-owner denial writes a SEL audit record (outcome="denied"), so a
        rejected attempt on the vault is not silent — matching the sibling
        agent-spec / aws-consent / messaging handlers."""
        app = _app()

        from unittest.mock import MagicMock

        from aiohttp.test_utils import TestClient, TestServer

        sel = MagicMock()
        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)),
            patch("kiro_crew.dashboard.handlers.secrets._sel", return_value=sel),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    headers={"X-Test-App": "some-app", "X-Test-User": "some-app-subject"},
                    json={"name": "EVIL", "value": "x"},
                )
                assert resp.status == 403
        sel.log_api_access.assert_called_once()
        kwargs = sel.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "secrets_set"
        assert kwargs["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_configured_owner_is_allowed(self, vault_dir: Path) -> None:
        app = _app()
        app["state"].owner_id = "U_OWNER"  # type: ignore[attr-defined]

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets", headers={"X-Test-User": "U_OWNER"})
                assert resp.status == 200


class TestApiSecretsLogInjection:
    """Control characters in the secret name must not reach the log verbatim (CWE-117).

    ``name`` is free-form user input trimmed only with ``.strip()``, which
    leaves interior ``\\n`` / ``\\r`` in place. Unsanitized, that forges extra
    log lines / fake audit entries. The handler escapes control characters
    before logging, so the emitted record stays on one line.
    """

    @pytest.mark.asyncio
    async def test_newline_in_set_name_is_escaped_in_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = _app()
        payload = "ok\nWARNING forged audit line"

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.handlers.secrets"):
                    resp = await client.post("/api/secrets", json={"name": payload, "value": "v"})
                    assert resp.status == 200

        stored = [r for r in caplog.records if "stored via dashboard" in r.getMessage()]
        assert stored, "expected a 'stored via dashboard' log record"
        msg = stored[0].getMessage()
        # The raw newline must not survive into the log line.
        assert "\n" not in msg
        assert "\\n" in msg

    @pytest.mark.asyncio
    async def test_crlf_in_delete_name_is_escaped_in_log(
        self, vault_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        # A percent-encoded CR/LF in the path segment decodes to real control
        # characters in request.match_info["name"].
        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.handlers.secrets"):
                    resp = await client.delete("/api/secrets/x%0d%0aWARNING-forged")
                    assert resp.status == 200

        deleted = [r for r in caplog.records if "deleted via dashboard" in r.getMessage()]
        assert deleted, "expected a 'deleted via dashboard' log record"
        msg = deleted[0].getMessage()
        assert "\n" not in msg
        assert "\r" not in msg


class TestApiSecretsSetInputValidation:
    """POST /api/secrets rejects well-formed JSON of the wrong shape with 400.

    These bodies all parse as valid JSON, so they get past the JSONDecodeError
    guard. Before the type checks, `body.get("name", "").strip()` raised
    AttributeError on each of them and surfaced as an HTTP 500.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ([{"name": "A", "value": "b"}], "invalid_body"),  # JSON array
            ("just a string", "invalid_body"),  # JSON string
            (42, "invalid_body"),  # JSON number
            ({"name": 123, "value": "b"}, "invalid_name_type"),  # non-string name
            ({"name": ["A"], "value": "b"}, "invalid_name_type"),  # list name
            ({"name": None, "value": "b"}, "invalid_name_type"),  # null name
            ({"value": "b"}, "invalid_name_type"),  # name absent entirely
            ({"name": "A", "value": 123}, "invalid_value_type"),  # non-string value
            ({"name": "A", "value": {"k": "v"}}, "invalid_value_type"),  # dict value
            ({"name": "A"}, "invalid_value_type"),  # value absent entirely
        ],
    )
    async def test_rejects_wrong_types_with_400(
        self, empty_vault_dir: Path, body: object, code: str
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json=body)
                assert resp.status == 400
                data = await resp.json()
                assert data["code"] == code
                # Nothing was written to the vault on a rejected request.
                assert SecretVault(empty_vault_dir).list_names() == []

    @pytest.mark.asyncio
    async def test_accepts_valid_string_payload(self, empty_vault_dir: Path) -> None:
        """The happy path still works after the added type checks."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "  PADDED  ", "value": "v"})
                assert resp.status == 200
                data = await resp.json()
                # Name is still trimmed, as before.
                assert data["name"] == "PADDED"
                assert SecretVault(empty_vault_dir).list_names() == ["PADDED"]
