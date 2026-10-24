"""Tests for kiro_crew.dashboard.handlers.secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web

from kiro_crew.config import loader as config_loader
from kiro_crew.config.loader import MANAGED_VAULT_FIXED_CONSUMERS
from kiro_crew.dashboard.handlers.secrets import (
    _managed_secret_catalog,
    _sanitize_for_log,
    _unused_stored_secrets,
    setup_secrets_routes,
)
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


_WAKATIME_MANAGED = {"name": "WAKATIME_API_KEY", "kind": "wakatime_api_key"}


class TestApiSecretsList:
    """Tests for GET /api/secrets."""

    def test_fixed_vault_consumer_registry_drives_catalog(self) -> None:
        catalog = _managed_secret_catalog([], ["example.com"], True, True)
        assert {entry["name"] for entry in catalog} == set(MANAGED_VAULT_FIXED_CONSUMERS)

    def test_unused_stored_reasons_follow_runtime_applicability(self) -> None:
        assert _unused_stored_secrets(["WAKATIME_API_KEY"], [], False, False) == [
            {"name": "WAKATIME_API_KEY", "reason": "wakatime_disabled"}
        ]
        assert _unused_stored_secrets(["JIRA_API_TOKEN"], ["a", "b"], False, True) == [
            {"name": "JIRA_API_TOKEN", "reason": "jira_multi_host"}
        ]
        host_name = config_loader.jira_host_token_name("example.com")
        assert _unused_stored_secrets(
            ["JIRA_API_TOKEN", host_name], ["example.com"], True, True
        ) == [{"name": "JIRA_API_TOKEN", "reason": "jira_host_precedence"}]

    @pytest.mark.asyncio
    async def test_lists_names_sorted(self, vault_dir: Path) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": ["DB_PASS", "TEST_KEY"], "managed": []}

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
                assert data == {"names": [], "managed": []}

    @pytest.mark.asyncio
    async def test_classifies_only_actual_jira_vault_consumers(self, tmp_path: Path) -> None:
        vault = SecretVault(tmp_path)
        vault._set_sync("JIRA_API_TOKEN", "global-token")
        vault._set_sync("JIRA_TOKEN_6578616D706C652E636F6D", "host-token")
        vault._set_sync("JIRA_TOKEN_6F727068616E2E636F6D", "orphan-token")
        vault._set_sync("JIRA_TOKEN_not_hex", "ordinary-entry")
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        jira_config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=True),
            dashboard=SimpleNamespace(jira_auth=[SimpleNamespace(host="example.com")]),
        )
        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=jira_config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()

        assert data["names"] == [
            "JIRA_API_TOKEN",
            "JIRA_TOKEN_6578616D706C652E636F6D",
            "JIRA_TOKEN_6F727068616E2E636F6D",
            "JIRA_TOKEN_not_hex",
        ]
        assert data["managed"] == [
            _WAKATIME_MANAGED,
            {
                "name": "JIRA_TOKEN_6578616D706C652E636F6D",
                "kind": "jira_host_token",
                "host": "example.com",
            },
        ]

    @pytest.mark.asyncio
    async def test_multiple_jira_hosts_omit_global_and_advertise_per_host_slots(
        self, tmp_path: Path
    ) -> None:
        app = _app()
        jira_config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=True),
            dashboard=SimpleNamespace(
                jira_auth=[
                    SimpleNamespace(host="One.Example:443"),
                    SimpleNamespace(host="two.example"),
                ]
            ),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=jira_config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                managed = (await resp.json())["managed"]

        assert managed == [
            _WAKATIME_MANAGED,
            {
                "name": "JIRA_TOKEN_6F6E652E6578616D706C65",
                "kind": "jira_host_token",
                "host": "one.example",
            },
            {
                "name": "JIRA_TOKEN_74776F2E6578616D706C65",
                "kind": "jira_host_token",
                "host": "two.example",
            },
        ]

    @pytest.mark.asyncio
    async def test_port_only_jira_entry_is_ignored(self, tmp_path: Path) -> None:
        app = _app()
        jira_config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=False),
            dashboard=SimpleNamespace(jira_auth=[SimpleNamespace(host=":443")]),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=jira_config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/secrets")
                assert response.status == 200
                assert await response.json() == {"names": [], "managed": []}

    @pytest.mark.asyncio
    async def test_single_whitespace_jira_entry_is_ignored(self, tmp_path: Path) -> None:
        app = _app()
        jira_config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=False),
            dashboard=SimpleNamespace(jira_auth=[SimpleNamespace(host="   ")]),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=jira_config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/secrets")
                assert response.status == 200
                assert await response.json() == {"names": [], "managed": []}

    @pytest.mark.asyncio
    async def test_config_failure_keeps_names_and_reports_managed_error(
        self, vault_dir: Path
    ) -> None:
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                side_effect=ValueError("broken config"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/secrets")
                assert response.status == 200
                assert await response.json() == {
                    "names": ["DB_PASS", "TEST_KEY"],
                    "managed": [],
                    "managed_error": True,
                }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("degraded", [{"*"}, {"dashboard"}, {"wakatime"}])
    async def test_degraded_managed_sections_report_managed_error(
        self, tmp_path: Path, degraded: set[str]
    ) -> None:
        app = _app()
        config = SimpleNamespace(
            degraded_sections=frozenset(degraded),
            wakatime=SimpleNamespace(enabled=False),
            dashboard=SimpleNamespace(jira_auth=[]),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/secrets")
                assert response.status == 200
                assert await response.json() == {
                    "names": [],
                    "managed": [],
                    "managed_error": True,
                }

    @pytest.mark.asyncio
    async def test_whitespace_jira_entry_uses_raw_count_for_applicability(
        self, tmp_path: Path
    ) -> None:
        app = _app()
        jira_config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=False),
            dashboard=SimpleNamespace(
                jira_auth=[
                    SimpleNamespace(host="example.com"),
                    SimpleNamespace(host="   "),
                ]
            ),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=jira_config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                managed = (await (await client.get("/api/secrets")).json())["managed"]

        assert managed == [
            {
                "name": "JIRA_TOKEN_6578616D706C652E636F6D",
                "kind": "jira_host_token",
                "host": "example.com",
            }
        ]

    @pytest.mark.asyncio
    async def test_duplicate_normalized_hosts_do_not_enable_global_fallback(
        self, tmp_path: Path
    ) -> None:
        app = _app()
        jira_config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=True),
            dashboard=SimpleNamespace(
                jira_auth=[
                    SimpleNamespace(host="Jira.Corp:443"),
                    SimpleNamespace(host="jira.corp"),
                ]
            ),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch(
                "kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load",
                return_value=jira_config,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                managed = (await (await client.get("/api/secrets")).json())["managed"]

        assert managed == [
            _WAKATIME_MANAGED,
            {
                "name": "JIRA_TOKEN_6A6972612E636F7270",
                "kind": "jira_host_token",
                "host": "jira.corp",
            },
        ]


_GERRIT_ONE = "gerrit.example.com:9080"
_GERRIT_ONE_NAME = "GERRIT_HTTP_CREDENTIAL_6765727269742E6578616D706C652E636F6D3A39303830"
_GERRIT_TWO = "gerritv.example.com:9081"
_GERRIT_TWO_NAME = "GERRIT_HTTP_CREDENTIAL_676572726974762E6578616D706C652E636F6D3A39303831"


class TestGerritManagedRows:
    """``dashboard.gerrit_hosts`` publishes one labelled row per host.

    The user fills the row by host and never derives the hex-suffixed vault
    name, which the provider's credential lookup still reads.
    """

    def test_no_hosts_no_rows(self) -> None:
        assert _managed_secret_catalog([], [], False, False, []) == []

    def test_lone_host_offers_the_global_name(self) -> None:
        catalog = _managed_secret_catalog([], [], False, False, [_GERRIT_ONE])
        assert catalog == [
            {
                "name": "GERRIT_HTTP_CREDENTIAL",
                "kind": "gerrit_http_credential",
                "host": _GERRIT_ONE,
            }
        ]

    def test_lone_host_with_stored_per_host_name_lists_that_name(self) -> None:
        """The provider prefers the per-host entry, so the row must edit that one."""
        catalog = _managed_secret_catalog([_GERRIT_ONE_NAME], [], False, False, [_GERRIT_ONE])
        assert catalog == [
            {"name": _GERRIT_ONE_NAME, "kind": "gerrit_http_credential", "host": _GERRIT_ONE}
        ]

    def test_two_hosts_get_per_host_rows_and_no_global_row(self) -> None:
        catalog = _managed_secret_catalog(
            ["GERRIT_HTTP_CREDENTIAL"], [], False, False, [_GERRIT_ONE, _GERRIT_TWO]
        )
        assert catalog == [
            {"name": _GERRIT_ONE_NAME, "kind": "gerrit_http_credential", "host": _GERRIT_ONE},
            {"name": _GERRIT_TWO_NAME, "kind": "gerrit_http_credential", "host": _GERRIT_TWO},
        ]

    def test_rows_follow_jira_rows_and_precede_connections(self) -> None:
        catalog = _managed_secret_catalog([], ["jira.corp"], False, True, [_GERRIT_ONE])
        assert [entry["kind"] for entry in catalog] == [
            "wakatime_api_key",
            "jira_host_token",
            "gerrit_http_credential",
        ]

    def test_global_name_unused_reasons(self) -> None:
        assert _unused_stored_secrets(
            ["GERRIT_HTTP_CREDENTIAL"], [], False, False, [_GERRIT_ONE, _GERRIT_TWO]
        ) == [{"name": "GERRIT_HTTP_CREDENTIAL", "reason": "gerrit_multi_host"}]
        assert _unused_stored_secrets(
            ["GERRIT_HTTP_CREDENTIAL", _GERRIT_ONE_NAME], [], False, False, [_GERRIT_ONE]
        ) == [{"name": "GERRIT_HTTP_CREDENTIAL", "reason": "gerrit_host_precedence"}]
        # A lone host served by the global name: nothing is unused.
        assert (
            _unused_stored_secrets(["GERRIT_HTTP_CREDENTIAL"], [], False, False, [_GERRIT_ONE])
            == []
        )
        # No hosts listed: the stored name is an ordinary user secret, not flagged.
        assert _unused_stored_secrets(["GERRIT_HTTP_CREDENTIAL"], [], False, False, []) == []

    @pytest.mark.asyncio
    async def test_list_reads_gerrit_hosts_from_config(self, tmp_path: Path) -> None:
        """The endpoint threads ``dashboard.gerrit_hosts`` into rows and unused hints."""
        vault = SecretVault(tmp_path)
        vault._set_sync("GERRIT_HTTP_CREDENTIAL", "user:pw")
        app = _app()
        config = SimpleNamespace(
            wakatime=SimpleNamespace(enabled=False),
            dashboard=SimpleNamespace(jira_auth=[], gerrit_hosts=[_GERRIT_TWO, _GERRIT_ONE]),
        )

        from aiohttp.test_utils import TestClient, TestServer

        with (
            patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)),
            patch("kiro_crew.dashboard.handlers.secrets.KiroCrewConfig.load", return_value=config),
        ):
            async with TestClient(TestServer(app)) as client:
                data = await (await client.get("/api/secrets")).json()

        assert data["managed"] == [
            {"name": _GERRIT_ONE_NAME, "kind": "gerrit_http_credential", "host": _GERRIT_ONE},
            {"name": _GERRIT_TWO_NAME, "kind": "gerrit_http_credential", "host": _GERRIT_TWO},
        ]
        assert data["unused"] == [{"name": "GERRIT_HTTP_CREDENTIAL", "reason": "gerrit_multi_host"}]

    def test_row_names_match_the_provider_lookup(self) -> None:
        """The row's name is exactly the vault name the provider reads for the host."""
        from kiro_crew.dashboard.source_providers import gerrit

        assert gerrit.host_credential_name(_GERRIT_ONE) == _GERRIT_ONE_NAME
        assert gerrit.GLOBAL_CREDENTIAL_NAME == "GERRIT_HTTP_CREDENTIAL"


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
    async def test_deletes_secret_removes_from_list(self, vault_dir: Path) -> None:
        """A successful DELETE removes the name from the vault; a subsequent list
        does not include it.  Proves the membership check does not block the
        actual deletion path."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/TEST_KEY")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                list_resp = await client.get("/api/secrets")
                assert list_resp.status == 200
                names = (await list_resp.json())["names"]
                assert "TEST_KEY" not in names
                assert "DB_PASS" in names  # sibling entry untouched

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, vault_dir: Path) -> None:
        """DELETE of a name that was never stored returns 404 not_found.

        Before the fix, vault.delete() was a silent no-op and the handler
        returned 200 ok unconditionally, hiding mistyped names from the caller.
        """
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/NONEXISTENT_NAME")
                assert resp.status == 404
                data = await resp.json()
                assert data["code"] == "not_found"
                assert "error" in data


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
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A percent-encoded CR/LF in the path segment decodes to real control
        characters in request.match_info["name"].  The handler must escape them
        before logging so the emitted record cannot forge extra audit lines.

        The name must actually exist in the vault so the handler reaches the
        log statement (a non-existent name returns 404 before logging).
        """
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        # Seed a vault entry whose name contains a literal CRLF.
        injected_name = "x\r\nWARNING-forged"
        vault = SecretVault(tmp_path)
        vault._set_sync(injected_name, "v")

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.handlers.secrets"):
                    # aiohttp URL-encodes the path when using client.delete(url)
                    # with a plain string, so percent-encode manually.
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


class TestApiSecretsSetWhitespaceValue:
    """POST /api/secrets rejects a value that is whitespace-only (F2 regression).

    Before the fix, ``name`` was stripped before its empty-check but ``value``
    was not — so a body like ``{"name":"K","value":"   "}`` bypassed the
    ``if not value:`` guard (a non-empty string of spaces is truthy) and wrote
    a blank secret to the vault.  After the fix, ``value = value.strip()`` is
    applied before the empty-check, so the three-space value collapses to ``""``
    and correctly hits the existing ``missing_value`` 400 path.
    """

    @pytest.mark.asyncio
    async def test_whitespace_only_value_is_rejected(self, empty_vault_dir: Path) -> None:
        """``{"name":"K","value":"   "}`` must return 400 missing_value."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "   "})
                assert resp.status == 400
                data = await resp.json()
                assert data["code"] == "missing_value"
                # The vault must not have stored K.
                assert SecretVault(empty_vault_dir).list_names() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ws_value", ["   ", "\t", "\n", " \t \n "])
    async def test_various_whitespace_variants_are_rejected(
        self, empty_vault_dir: Path, ws_value: str
    ) -> None:
        """Tabs, newlines, and mixed whitespace all reduce to empty after strip."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": ws_value})
                assert resp.status == 400
                data = await resp.json()
                assert data["code"] == "missing_value"
                assert SecretVault(empty_vault_dir).list_names() == []

    @pytest.mark.asyncio
    async def test_normal_value_still_stores(self, empty_vault_dir: Path) -> None:
        """Positive regression: a real value continues to be stored correctly."""
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": "real-value"})
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert SecretVault(empty_vault_dir).list_names() == ["K"]

    @pytest.mark.asyncio
    async def test_padded_value_is_stored_unchanged(self, empty_vault_dir: Path) -> None:
        """A value with real content plus surrounding whitespace is stored VERBATIM.

        The emptiness check uses ``value.strip()``, but the stored value must be
        the original, untrimmed string — a credential can legitimately carry
        leading/trailing whitespace and trimming it would corrupt the secret.
        """
        app = _app()

        from aiohttp.test_utils import TestClient, TestServer

        padded = "  sk-live-abc  "
        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "K", "value": padded})
                assert resp.status == 200
                # The vault must hold the ORIGINAL padded value, byte-for-byte.
                assert SecretVault(empty_vault_dir).get("K").reveal() == padded


class TestSanitizeForLog:
    """Unit tests for the module-private _sanitize_for_log helper.

    The asserts for ESC (\\x1b), NUL (\\x00), and DEL (\\x7f) are designed to
    FAIL on origin/main (which only escaped \\n/\\r/\\t) and PASS after the
    full C0+DEL fix that adds the _CONTROL_CHAR_RE substitution.
    """

    def test_newline_carriage_return_tab_escaped_as_two_char(self) -> None:
        r"""\\n, \\r, \\t map to their familiar two-character spellings."""
        assert _sanitize_for_log("\n") == "\\n"
        assert _sanitize_for_log("\r") == "\\r"
        assert _sanitize_for_log("\t") == "\\t"
        assert _sanitize_for_log("a\nb\rc\td") == "a\\nb\\rc\\td"

    def test_ansi_escape_sequence_escaped(self) -> None:
        r"""ESC (\\x1b) in an ANSI colour code becomes \\x1b; printable chars kept."""
        # ESC [ 3 1 m X -- the ANSI red-colour prefix followed by 'X'
        result = _sanitize_for_log("\x1b[31mX")
        # ESC must be escaped; the printable '[31mX' must be preserved verbatim
        assert result == "\\x1b[31mX", repr(result)

    def test_nul_byte_escaped(self) -> None:
        r"""NUL (\\x00) becomes \\x00."""
        result = _sanitize_for_log("\x00")
        assert result == "\\x00", repr(result)

    def test_del_escaped(self) -> None:
        r"""DEL (\\x7f) becomes \\x7f."""
        result = _sanitize_for_log("\x7f")
        assert result == "\\x7f", repr(result)

    def test_backslash_escaped_first_no_double_transform(self) -> None:
        r"""A literal backslash becomes \\\\ and is not re-processed."""
        # A single backslash in the input must yield exactly two backslashes out.
        result = _sanitize_for_log("\\")
        assert result == "\\\\", repr(result)
        # Backslash before n must become \\\\n (escaped backslash + literal n),
        # not \\n (which would look like an escaped newline).
        result2 = _sanitize_for_log("\\n")
        assert result2 == "\\\\n", repr(result2)

    def test_printable_ascii_unchanged(self) -> None:
        """Ordinary printable text passes through unmodified."""
        plain = "hello-WORLD_123 /path/to/key"
        assert _sanitize_for_log(plain) == plain

    def test_mixed_controls_and_printable(self) -> None:
        r"""A string mixing printable, \\n, and an ANSI escape is fully sanitized."""
        inp = "key\x1b[0m\nname"
        result = _sanitize_for_log(inp)
        assert result == "key\\x1b[0m\\nname", repr(result)

    def test_c1_control_escaped(self) -> None:
        r"""A C1 control (e.g. CSI \\x9b) is escaped, not passed to the terminal."""
        result = _sanitize_for_log("\x9b")
        assert result == "\\x9b", repr(result)

    def test_unicode_line_separators_escaped(self) -> None:
        r"""U+2028 / U+2029 (Unicode line/paragraph separators) are escaped.

        A Unicode-aware log viewer treats these as line breaks, so they are a
        log-injection vector just like \\n; they must not survive verbatim.
        """
        assert _sanitize_for_log("\u2028") == "\\u2028", repr(_sanitize_for_log("\u2028"))
        assert _sanitize_for_log("\u2029") == "\\u2029", repr(_sanitize_for_log("\u2029"))
        # A forged-line payload via U+2028 is neutralized.
        result = _sanitize_for_log("ok\u2028WARNING forged")
        assert "\u2028" not in result and "\\u2028" in result, repr(result)


# A proxied request: loopback peer, but a forwarding header, so
# ``is_direct_local_request`` reads it as a remote or tunnelled session.
_REMOTE = {"X-Forwarded-For": "203.0.113.7"}


class TestApiSecretsChannelCredentialRemote:
    """A remote owner cannot set or delete a vault entry a channel token resolves from.

    The channel settings panels accept a save only from the machine running the
    gateway; the generic secrets route must not be a way around that gate.
    """

    @pytest.fixture(autouse=True)
    def _clean_channel_env(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.handlers.secrets import _CHANNEL_ENV_KEYS

        for key in _CHANNEL_ENV_KEYS:
            _floor_monkeypatch.delenv(key, raising=False)

    @staticmethod
    async def _call(
        home: Path, method: str, name: str, *, headers: dict[str, str] | None = None
    ) -> tuple[int, dict]:
        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(home)):
            async with TestClient(TestServer(_app())) as client:
                if method == "set":
                    resp = await client.post(
                        "/api/secrets",
                        json={"name": name, "value": "planted"},
                        headers=headers or {},
                    )
                else:
                    resp = await client.delete(f"/api/secrets/{name}", headers=headers or {})
                return resp.status, await resp.json()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["set", "delete"])
    async def test_remote_write_to_channel_key_name_is_refused(
        self, tmp_path: Path, method: str
    ) -> None:
        SecretVault(tmp_path)._set_sync("SLACK_BOT_TOKEN", "xoxb-original")

        status, data = await self._call(tmp_path, method, "SLACK_BOT_TOKEN", headers=_REMOTE)

        assert status == 409
        assert data["code"] == "managed_by_channels"
        assert "Channels" in data["error"]
        assert SecretVault(tmp_path).get("SLACK_BOT_TOKEN").reveal() == "xoxb-original"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["set", "delete"])
    async def test_remote_write_to_name_a_channel_env_line_references_is_refused(
        self, tmp_path: Path, method: str
    ) -> None:
        (tmp_path / ".env").write_text(
            "# channel\nTELEGRAM_BOT_TOKEN = secret://MY_TG_TOKEN\n", encoding="utf-8"
        )
        SecretVault(tmp_path)._set_sync("MY_TG_TOKEN", "tg-original")

        status, data = await self._call(tmp_path, method, "MY_TG_TOKEN", headers=_REMOTE)

        assert status == 409
        assert data["code"] == "managed_by_channels"
        assert SecretVault(tmp_path).get("MY_TG_TOKEN").reveal() == "tg-original"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["set", "delete"])
    async def test_remote_write_to_name_a_channel_env_var_references_is_refused(
        self, tmp_path: Path, method: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret://MY_DC_TOKEN")
        SecretVault(tmp_path)._set_sync("MY_DC_TOKEN", "dc-original")

        status, data = await self._call(tmp_path, method, "MY_DC_TOKEN", headers=_REMOTE)

        assert status == 409
        assert SecretVault(tmp_path).get("MY_DC_TOKEN").reveal() == "dc-original"

    @pytest.mark.asyncio
    async def test_unreadable_env_fails_closed_for_remote_write(self, tmp_path: Path) -> None:
        (tmp_path / ".env").mkdir()

        status, data = await self._call(tmp_path, "set", "ANY_NAME", headers=_REMOTE)

        assert status == 409
        assert data["code"] == "managed_by_channels"
        assert SecretVault(tmp_path).get("ANY_NAME") is None

    @pytest.mark.asyncio
    async def test_direct_local_set_and_delete_of_channel_name_still_work(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=secret://SLACK_BOT_TOKEN\n", encoding="utf-8"
        )

        status, _ = await self._call(tmp_path, "set", "SLACK_BOT_TOKEN")
        assert status == 200
        assert SecretVault(tmp_path).get("SLACK_BOT_TOKEN").reveal() == "planted"

        status, _ = await self._call(tmp_path, "delete", "SLACK_BOT_TOKEN")
        assert status == 200
        assert SecretVault(tmp_path).get("SLACK_BOT_TOKEN") is None

    @pytest.mark.asyncio
    async def test_remote_set_and_delete_of_non_channel_name_still_work(
        self, tmp_path: Path
    ) -> None:
        # A non-channel credential's reference (the Jira token the Secrets panel
        # manages) does not make its vault name channel-owned.
        (tmp_path / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=secret://MY_TG_TOKEN\nJIRA_API_TOKEN=secret://MY_JIRA\n",
            encoding="utf-8",
        )

        for name in ("MY_JIRA", "OTHER_KEY"):
            status, _ = await self._call(tmp_path, "set", name, headers=_REMOTE)
            assert status == 200, name
            assert SecretVault(tmp_path).get(name).reveal() == "planted"

            status, _ = await self._call(tmp_path, "delete", name, headers=_REMOTE)
            assert status == 200, name
            assert SecretVault(tmp_path).get(name) is None
