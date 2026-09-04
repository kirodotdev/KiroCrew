"""Tests for the Teams config API handlers (GET/PUT /api/teams/config).

Mirrors ``test_webex_config_handlers.py``: the Teams panel is built against this
contract, so the field set, the masking invariant (presence booleans only, never a
raw secret), the direct-local write gate, and the validate-first/commit-last
ordering are pinned here rather than left to the frontend to discover.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod
import kiro_crew.teams.client as teams_client

_CRED_KEYS = ("MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "MICROSOFT_APP_TENANT_ID")


@pytest.fixture(autouse=True)
def _no_inherited_credentials(monkeypatch: pytest.MonkeyPatch):
    """``load_credentials`` lets ``os.environ`` win over .env, and the save path
    writes into it, so the process environment is shared state here."""
    for key in _CRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _CRED_KEYS:
        os.environ.pop(key, None)


class _StubRequest:
    """Request double for the config handlers: real ``json()``, ``get()``, ``app``."""

    def __init__(self, body: dict | None = None, state: Any = None) -> None:
        self._body = body or {}
        self.app: dict[str, Any] = {"state": state}

    async def json(self) -> dict:
        return self._body

    def get(self, key: str, default: Any = None) -> Any:
        return default


class _State:
    teams_connected = True
    teams_connect_error = ""


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    env = tmp_path / ".env"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    return env, cfg_path


def _get(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: dict) -> dict:
    """Drive api_teams_config_get against an isolated config.json."""
    _env, cfg_path = _isolate(monkeypatch, tmp_path)
    cfg_path.write_text(json.dumps(config), encoding="utf-8")
    resp = asyncio.run(mod.api_teams_config_get(_StubRequest(state=_State())))
    assert resp.status == 200
    return json.loads(resp.body)


def _save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: dict,
    *,
    verify: Any = None,
    calls: list[tuple[str, str, str]] | None = None,
) -> tuple[Any, Path, Path]:
    """Drive api_teams_config_save against isolated .env + config.json."""
    env, cfg_path = _isolate(monkeypatch, tmp_path)

    async def _fake_verify(app_id: str, app_password: str, tenant_id: str):
        if calls is not None:
            calls.append((app_id, app_password, tenant_id))
        if isinstance(verify, Exception):
            raise verify
        return verify

    monkeypatch.setattr(mod, "_validate_teams_app_credentials", _fake_verify)
    resp = asyncio.run(mod.api_teams_config_save(_StubRequest(body)))
    return resp, env, cfg_path


# ── GET: the panel's contract ──


class TestGet:
    def test_reports_every_contract_field(self, monkeypatch, tmp_path: Path) -> None:
        payload = _get(
            monkeypatch,
            tmp_path,
            {
                "teams": {
                    "enabled": True,
                    "app_id": "app-123",
                    "tenant_id": "tenant-1",
                    "allowed_emails": ["you@example.com"],
                    "soft_threshold_pct": 70,
                    "hard_threshold_pct": 90,
                    "session_folder": "Teams",
                }
            },
        )
        assert set(payload) == {
            "connected",
            "connect_error",
            "configured",
            "read_only",
            "app_id_set",
            "app_password_set",
            "jwt_available",
            "enabled",
            "tenant_id",
            "allowed_emails",
            "soft_threshold_pct",
            "hard_threshold_pct",
            "session_folder",
        }
        assert payload["enabled"] is True
        assert payload["tenant_id"] == "tenant-1"
        assert payload["allowed_emails"] == ["you@example.com"]
        assert payload["soft_threshold_pct"] == 70
        assert payload["hard_threshold_pct"] == 90
        assert payload["session_folder"] == "Teams"

    def test_jwt_available_tracks_the_optional_extra(self, monkeypatch, tmp_path: Path) -> None:
        """The channel refuses to start without PyJWT, so a panel that cannot show
        this leaves the operator with a channel that silently never starts."""
        monkeypatch.setattr(teams_client, "HAS_JWT", False)
        assert _get(monkeypatch, tmp_path, {})["jwt_available"] is False
        monkeypatch.setattr(teams_client, "HAS_JWT", True)
        assert _get(monkeypatch, tmp_path, {})["jwt_available"] is True

    def test_thresholds_default_when_unset(self, monkeypatch, tmp_path: Path) -> None:
        payload = _get(monkeypatch, tmp_path, {})
        assert payload["soft_threshold_pct"] == 80
        assert payload["hard_threshold_pct"] == 95

    def test_tenant_id_prefers_the_env_credential(self, monkeypatch, tmp_path: Path) -> None:
        """The boot path resolves the tenant as env-then-config, so a panel that
        reported only config.json would show a blank (multi-tenant) tenant for a
        bot that is in fact single-tenant."""
        monkeypatch.setenv("MICROSOFT_APP_TENANT_ID", "env-tenant")
        payload = _get(monkeypatch, tmp_path, {"teams": {"tenant_id": "config-tenant"}})
        assert payload["tenant_id"] == "env-tenant"

    def test_never_returns_the_secret(self, monkeypatch, tmp_path: Path) -> None:
        """Presence only, and deliberately no masked preview: an Azure client
        secret carries no vendor prefix for the shared mask to keep."""
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "s3cr3t-value-abcd")
        monkeypatch.setenv("MICROSOFT_APP_ID", "app-from-env")
        _env, cfg_path = _isolate(monkeypatch, tmp_path)
        cfg_path.write_text(json.dumps({"teams": {}}), encoding="utf-8")
        resp = asyncio.run(mod.api_teams_config_get(_StubRequest(state=_State())))
        raw = resp.body.decode("utf-8")
        assert "s3cr3t-value-abcd" not in raw
        assert json.loads(raw)["app_password_set"] is True
        assert "app_password_preview" not in json.loads(raw)

    def test_configured_requires_credentials_enabled_and_an_allow_list(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MICROSOFT_APP_ID", "app-1")
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "secret")
        base = {"enabled": True, "allowed_emails": ["you@example.com"]}
        assert _get(monkeypatch, tmp_path, {"teams": base})["configured"] is True
        # An empty allow-list denies everyone, so the channel is not configured.
        assert (
            _get(monkeypatch, tmp_path, {"teams": {**base, "allowed_emails": []}})["configured"]
            is False
        )
        assert (
            _get(monkeypatch, tmp_path, {"teams": {**base, "enabled": False}})["configured"]
            is False
        )

    def test_read_only_for_remote_sessions(self, monkeypatch, tmp_path: Path) -> None:
        _env, cfg_path = _isolate(monkeypatch, tmp_path)
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        resp = asyncio.run(mod.api_teams_config_get(_StubRequest(state=_State())))
        assert json.loads(resp.body)["read_only"] is True


# ── PUT: thresholds ──


class TestSaveThresholds:
    def test_saves_both_thresholds(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, cfg_path = _save(
            monkeypatch, tmp_path, {"soft_threshold_pct": 60, "hard_threshold_pct": 85}
        )
        assert resp.status == 200
        assert json.loads(resp.body)["restart_required"] is True
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["teams"]["soft_threshold_pct"] == 60
        assert data["teams"]["hard_threshold_pct"] == 85

    @pytest.mark.parametrize("value", [0, 101, -1, "80", 80.5, None, True])
    def test_rejects_out_of_range_and_non_int(
        self, monkeypatch, tmp_path: Path, value: Any
    ) -> None:
        """``isinstance(True, int)`` is True in Python, so a JSON ``true`` must be
        refused rather than read as 1%."""
        resp, _env, cfg_path = _save(monkeypatch, tmp_path, {"soft_threshold_pct": value})
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "soft_threshold_pct_invalid"
        assert not cfg_path.exists()

    def test_rejects_hard_below_soft(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, cfg_path = _save(
            monkeypatch, tmp_path, {"soft_threshold_pct": 90, "hard_threshold_pct": 50}
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "threshold_pct_inverted"
        assert not cfg_path.exists()

    def test_one_half_is_validated_against_the_stored_other_half(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The panel may send a single field, so the ordering rule has to consider
        the effective pair, not just what arrived."""
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"soft_threshold_pct": 80, "hard_threshold_pct": 90}}),
            encoding="utf-8",
        )
        resp, _env, cfg_path = _save(monkeypatch, tmp_path, {"soft_threshold_pct": 95})
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "threshold_pct_inverted"
        # Commit-last: the rejected save left the stored pair untouched.
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["teams"] == {"soft_threshold_pct": 80, "hard_threshold_pct": 90}

    def test_equal_thresholds_are_accepted(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, cfg_path = _save(
            monkeypatch, tmp_path, {"soft_threshold_pct": 90, "hard_threshold_pct": 90}
        )
        assert resp.status == 200
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["teams"]["soft_threshold_pct"] == 90

    def test_unchanged_threshold_needs_no_restart(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"soft_threshold_pct": 70}}), encoding="utf-8"
        )
        resp, _env, _cfg = _save(monkeypatch, tmp_path, {"soft_threshold_pct": 70})
        assert resp.status == 200
        assert json.loads(resp.body)["restart_required"] is False


# ── PUT: credential verification ──


class TestSaveCredentialVerification:
    def test_rejected_credentials_block_the_save(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"app_id": "app-1", "app_password": "wrong", "enabled": True},
            verify="invalid_client",
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "credentials_rejected"
        assert not env.exists()  # nothing persisted
        assert not cfg_path.exists()

    def test_a_triple_that_changed_under_us_is_refused_not_stored(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Nothing unverified is stored, even when a concurrent save moved the other half.

        The verification is a network round trip, so it cannot hold the repo-wide config
        lock -- that lock serializes every writer in the process, and a hung Azure
        endpoint would wedge them. So it verifies outside and CONFIRMS inside. Without
        the confirmation, two concurrent saves (one changing the app id, one the secret)
        each verify a triple containing the other's old value, pass, and then merge on
        commit into a triple neither checked: a green "Saved." and a dead channel at the
        next restart.

        Simulated by having the verifier itself write a competing app id, which is what a
        racing save would have done between the check and the commit.
        """
        env, cfg_path = _isolate(monkeypatch, tmp_path)
        cfg_path.write_text(json.dumps({"teams": {"app_id": "app-original"}}), encoding="utf-8")
        verified: list[str] = []

        async def _verify_then_race(app_id: str, app_password: str, tenant_id: str):
            verified.append(app_id)
            cfg_path.write_text(
                json.dumps({"teams": {"app_id": "app-from-the-other-save"}}),
                encoding="utf-8",
            )
            return None

        monkeypatch.setattr(mod, "_validate_teams_app_credentials", _verify_then_race)
        resp = asyncio.run(
            mod.api_teams_config_save(_StubRequest({"app_password": "verified-secret"}))
        )

        assert verified == ["app-original"], "it verified the id it could see"
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "config_changed"
        assert not env.exists(), "the secret must not be stored against an unchecked id"
        assert json.loads(cfg_path.read_text(encoding="utf-8"))["teams"] == {
            "app_id": "app-from-the-other-save"
        }, "and the other save's value must survive untouched"

    def test_an_unraced_save_still_commits(self, monkeypatch, tmp_path: Path) -> None:
        """The confirmation must not refuse the ordinary single-save path."""
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"app_id": "app-1", "app_password": "good", "tenant_id": "t-1"},
        )

        assert resp.status == 200
        assert "MICROSOFT_APP_PASSWORD=good" in env.read_text(encoding="utf-8")

    def test_unreachable_azure_saves_with_a_warning(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _cfg = _save(
            monkeypatch,
            tmp_path,
            {"app_id": "app-1", "app_password": "maybe-good"},
            verify=RuntimeError("network down"),
        )
        assert resp.status == 200
        assert json.loads(resp.body)["verify_warning"]
        assert "MICROSOFT_APP_PASSWORD=maybe-good" in env.read_text(encoding="utf-8")

    def test_accepted_credentials_save_clean(self, monkeypatch, tmp_path: Path) -> None:
        calls: list[tuple[str, str, str]] = []
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"app_id": "app-1", "app_password": "good", "tenant_id": "tenant-9"},
            calls=calls,
        )
        assert resp.status == 200
        assert json.loads(resp.body)["verify_warning"] == ""
        assert calls == [("app-1", "good", "tenant-9")]
        assert "MICROSOFT_APP_PASSWORD=good" in env.read_text(encoding="utf-8")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["teams"]["app_id"] == "app-1"
        # The secret is env-only: config.json must never hold it.
        assert "app_password" not in data["teams"]

    def test_env_write_failure_restores_prior_config(self, monkeypatch, tmp_path: Path) -> None:
        """A failed .env credential write must roll config.json back.

        Config metadata is written BEFORE the .env credential. If the .env write
        then fails, the new app_id would otherwise be left paired with the OLD
        password on disk (a broken pair that fails Teams auth on restart). The
        handler snapshots config before writing and restores it on .env failure,
        so the persisted pair stays consistent (old app_id + old password).
        """
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"teams": {"app_id": "app-original", "tenant_id": "t-1"}}),
            encoding="utf-8",
        )

        def _boom(_updates):
            raise OSError("disk full writing .env")

        monkeypatch.setattr(mod, "_write_env_updates", _boom)

        with pytest.raises(OSError):
            _save(
                monkeypatch,
                tmp_path,
                {"app_id": "app-new", "app_password": "new-secret", "tenant_id": "t-1"},
            )

        # config.json restored to the pre-save app_id, not left with "app-new".
        restored = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert restored["teams"]["app_id"] == "app-original"

    def test_a_pasted_secret_is_verified_against_the_stored_app_id(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"app_id": "stored-app", "tenant_id": "stored-tenant"}}),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, str]] = []
        resp, _env, _cfg = _save(monkeypatch, tmp_path, {"app_password": "fresh"}, calls=calls)
        assert resp.status == 200
        assert calls == [("stored-app", "fresh", "stored-tenant")]

    def test_no_credential_change_skips_the_network_call(self, monkeypatch, tmp_path: Path) -> None:
        """Toggling the channel or editing the allow-list must not reach Azure."""
        calls: list[tuple[str, str, str]] = []
        resp, _env, _cfg = _save(
            monkeypatch,
            tmp_path,
            {"enabled": True, "allowed_emails": ["you@example.com"]},
            calls=calls,
        )
        assert resp.status == 200
        assert calls == []

    def test_clearing_the_secret_verifies_nothing(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("MICROSOFT_APP_PASSWORD=old\n", encoding="utf-8")
        calls: list[tuple[str, str, str]] = []
        resp, env, _cfg = _save(monkeypatch, tmp_path, {"app_password_clear": True}, calls=calls)
        assert resp.status == 200
        assert calls == []
        assert "MICROSOFT_APP_PASSWORD" not in env.read_text(encoding="utf-8")


# ── PUT: gates that must not regress ──


class TestSaveGates:
    def test_denies_non_loopback(self, monkeypatch, tmp_path: Path) -> None:
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        resp = asyncio.run(mod.api_teams_config_save(_StubRequest({"app_password": "planted"})))
        assert resp.status == 403

    def test_clear_flag_is_a_strict_boolean(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _cfg = _save(monkeypatch, tmp_path, {"app_password_clear": "yes"})
        assert resp.status == 400
        assert not env.exists()

    def test_secret_with_whitespace_rejected(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _cfg = _save(monkeypatch, tmp_path, {"app_password": "has space"})
        assert resp.status == 400
        assert not env.exists()

    def test_invalid_principal_rejected(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, cfg_path = _save(monkeypatch, tmp_path, {"allowed_emails": ["not a principal"]})
        assert resp.status == 400
        assert not cfg_path.exists()


# ── The verifier's own status classification ──


class TestCredentialVerifier:
    """Which status means "wrong credentials" and which means "Azure is having
    trouble" is what decides reject-versus-warn, so it is driven against a real
    HTTP server rather than a mocked session."""

    @staticmethod
    async def _probe(
        monkeypatch: pytest.MonkeyPatch,
        status: int,
        payload: dict | None,
        tenant: str = "tenant-1",
        seen: list[str] | None = None,
    ):
        from aiohttp import web as aioweb
        from aiohttp.test_utils import TestServer

        async def _token(request: Any) -> Any:
            if seen is not None:
                seen.append(request.match_info["tenant"])
            if payload is None:
                return aioweb.Response(status=status)
            return aioweb.json_response(payload, status=status)

        app = aioweb.Application()
        app.router.add_post("/{tenant}/token", _token)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setattr(
                teams_client,
                "_TOKEN_URL_TMPL",
                f"http://{server.host}:{server.port}/{{tenant}}/token",
            )
            return await mod._validate_teams_app_credentials("app-1", "secret", tenant)
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_a_token_means_the_credentials_are_good(self, monkeypatch) -> None:
        assert await self._probe(monkeypatch, 200, {"access_token": "t"}) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_azure_refusal_is_a_rejection(self, monkeypatch, status: int) -> None:
        result = await self._probe(
            monkeypatch,
            status,
            {
                "error": "invalid_client",
                "error_description": "AADSTS7000215 correlation_id: 1234-abcd",
            },
        )
        assert result == "invalid_client"
        # The description carries tenant/app ids and a correlation id; only the
        # machine-readable code is surfaced.
        assert "AADSTS7000215" not in (result or "")

    @pytest.mark.asyncio
    async def test_a_refusal_without_json_falls_back_to_the_status(self, monkeypatch) -> None:
        assert await self._probe(monkeypatch, 403, None) == "HTTP 403"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_azure_side_trouble_is_unverifiable_not_invalid(
        self, monkeypatch, status: int
    ) -> None:
        """Raising is what makes the caller save with ``verify_warning`` instead of
        refusing a credential that may well be correct."""
        with pytest.raises(RuntimeError):
            await self._probe(monkeypatch, status, None)

    @pytest.mark.asyncio
    async def test_a_blank_tenant_uses_the_multitenant_authority(self, monkeypatch) -> None:
        """A bot with no tenant is multi-tenant, and its client-credentials token
        comes from the Bot Framework authority — the same substitution the running
        client makes, so the check exercises the real endpoint."""
        from kiro_crew.teams.client import TEAMS_MULTITENANT_AUTHORITY

        seen: list[str] = []
        await self._probe(monkeypatch, 200, {"access_token": "t"}, tenant="  ", seen=seen)
        # Read from teams.client, not a handler-local copy: one owning module is
        # what keeps the save-time check and the boot path on the same authority.
        assert seen == [TEAMS_MULTITENANT_AUTHORITY]
