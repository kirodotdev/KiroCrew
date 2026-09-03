"""Tests for kiro_crew.dashboard.handlers.secrets."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.secrets import _sanitize_for_log, setup_secrets_routes
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
    async def test_deletes_secret_removes_from_list(self, vault_dir: Path) -> None:
        """A successful DELETE removes the name from the vault; a subsequent list
        no longer includes it.  Proves the membership check does not block the
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
