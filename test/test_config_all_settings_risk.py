"""Settings -> Advanced: the safety, sign-in, reach and share keys editable with a risk line."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.dashboard.handlers import config_all_settings as all_settings
from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG

_BY_PATH = {e.path: e for e in SCHEMA_REGISTRY}
_REPO = Path(__file__).resolve().parent.parent
_EN_CATALOG = _REPO / "website" / "src" / "i18n" / "locales" / "en.manual.json"
_RISK_MAP = _REPO / "website" / "src" / "components" / "allSettings" / "allSettingsRisk.ts"
_RISK_REASONS = {
    all_settings.WHY_SAFETY,
    all_settings.WHY_SIGN_IN,
    all_settings.WHY_REACH,
    all_settings.WHY_SHARE,
}

pytestmark = pytest.mark.asyncio


def _risk_lines() -> dict[str, str]:
    catalog = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))
    return catalog["settings"]["advanced"]["risk"]


def _risk_map() -> dict[str, str]:
    text = _RISK_MAP.read_text(encoding="utf-8")
    return dict(
        re.findall(r"^\s*'([^']+)': '(settings\.advanced\.risk\.[a-z0-9_]+)',$", text, re.M)
    )


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    request.app["state"].owner_id = ""
    return await handler(request)


@web.middleware
async def _app_token_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = "some-app"
    request.app["state"].owner_id = ""
    return await handler(request)


def _make_app(identity=_owner_identity) -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


def _stored_at(cfg_path, path: str) -> object:
    node: object = json.loads(cfg_path.read_text(encoding="utf-8"))
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


async def _send(body: dict, identity=_owner_identity) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(identity))) as client:
        resp = await client.patch("/api/config/kirocrew", json=body)
        return resp.status, await resp.json()


# ── Every editable risk key has its line, on both sides ──


def test_every_risk_key_is_a_real_key_with_a_risk_reason() -> None:
    for path in all_settings.RISK_NOTE_PATHS:
        assert path in _BY_PATH, path
        assert all_settings.READONLY_REASONS.get(path) in _RISK_REASONS, path


def test_every_risk_key_has_a_written_line_and_a_page_mapping() -> None:
    """A key becomes editable here only with a risk line someone wrote for it."""
    lines, mapping = _risk_lines(), _risk_map()
    for path in sorted(all_settings.RISK_NOTE_PATHS):
        slug = path.replace(".", "_")
        assert lines.get(slug, "").strip(), f"no risk line for {path}"
        assert mapping.get(path) == f"settings.advanced.risk.{slug}", path
    assert set(mapping) == set(all_settings.RISK_NOTE_PATHS)
    assert set(lines) == {p.replace(".", "_") for p in all_settings.RISK_NOTE_PATHS}


def test_a_risk_line_is_one_short_line() -> None:
    for slug, text in _risk_lines().items():
        assert "\n" not in text and len(text) <= 120, slug


@pytest.mark.parametrize("path", sorted(all_settings.RISK_NOTE_PATHS))
def test_a_risk_key_is_editable_and_names_its_risk(path: str) -> None:
    record = all_settings.descriptor(_BY_PATH[path])
    assert record is not None and record["mode"] == "edit", record
    assert record["risk"] == all_settings.READONLY_REASONS[path]


def test_a_risk_key_with_a_risk_reason_but_no_line_stays_read_only(monkeypatch) -> None:
    """Removing a key's risk line entry puts it back to read-only: the line is the gate."""
    monkeypatch.setattr(
        all_settings, "RISK_NOTE_PATHS", all_settings.RISK_NOTE_PATHS - {"tunnel.enabled"}
    )
    monkeypatch.setattr(
        all_settings, "READONLY_PATHS", all_settings.READONLY_PATHS | {"tunnel.enabled"}
    )
    assert all_settings.descriptor(_BY_PATH["tunnel.enabled"]) == {
        "mode": "readonly",
        "reason": all_settings.WHY_REACH,
    }
    assert all_settings.generic_spec("tunnel.enabled") is None


def test_no_risk_key_reaches_the_change_card_table() -> None:
    """A change card reads only ``_EDITABLE_CONFIG``; no generic rule is added there."""
    from kiro_crew.change_card_catalog import CardCatalogError
    from kiro_crew.dashboard.change_cards_settings import resolve_setting

    for path in ("agent.dangerously_skip_permissions", "tunnel.enabled", "agent.jail"):
        assert path not in _EDITABLE_CONFIG
        with pytest.raises(CardCatalogError):
            resolve_setting({"path": path})


# ── The PATCH route ──


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("agent.deny_override", False),
        ("tunnel.enabled", True),
        ("messaging.dm_scope", "unified"),
        ("telemetry.traces_enabled", True),
        ("wakatime.enabled", True),
        ("agent.subagent_timeout_secs", 0),
        ("agent.subagent_timeout_secs", 7200),
        ("dashboard.github_hosts", ["github.example.com"]),
        ("dashboard.gitlab_hosts", ["gitlab.example.com:8443"]),
        ("dashboard.avatar", "~/pictures/me.png"),
        ("tunnel.relay_lease_url", "https://lease.example.com"),
        ("tunnel.relay_lease_url", "http://127.0.0.1:9000"),
        ("tunnel.relay_lease_token_type", "API_KEY"),
        ("mcp_gateway.pool_identity_env", ["AWS_PROFILE"]),
        ("agent.strands_project_sha256", "a" * 64),
        ("publish.allowed_destinations", ["s3"]),
    ],
)
async def test_the_owner_saves_a_risk_key(tmp_config, path, value) -> None:
    status, body = await _send({"path": path, "value": value})
    assert status == 200, body
    assert _stored_at(tmp_config, path) == value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # A non-zero timeout under the loader's floor would read back raised and
        # log a tamper event.
        ("agent.subagent_timeout_secs", 30),
        ("agent.sandbox_readonly_paths", ["relative"]),
        ("dashboard.github_hosts", ["https://github.example.com"]),
        ("dashboard.github_hosts", ["github.example.com:8443"]),
        ("dashboard.gitlab_hosts", ["gitlab.example.com:99999"]),
        ("knowledge.doc_ingest_hosts", ["docs.example.com/path"]),
        # The logo route serves this file to anyone who can open the sign-in page.
        ("dashboard.avatar", "~/.ssh/config"),
        ("tunnel.relay_lease_url", "http://lease.example.com"),
        ("wakatime.api_base_url", "http://wakapi.example.com/api"),
        ("telemetry.beacon_endpoint", "http://beacon.example.com"),
        ("tunnel.relay_lease_token_type", "PASSWORD"),
        ("tunnel.relay_viewer_oidc_scope_prefix", "Has-Caps"),
        ("mcp_gateway.pool_identity_env", ["NOT A NAME"]),
        ("agent.strands_project_sha256", "abc"),
        ("dashboard.shared_crew.ssm_port", 80),
        ("agent.deny_override", "yes"),
    ],
)
async def test_patch_refuses_a_risk_value_its_reader_cannot_use(tmp_config, path, value) -> None:
    status, body = await _send({"path": path, "value": value})
    assert status == 400, body
    assert _stored_at(tmp_config, path) is None


async def test_the_declared_skip_approvals_grant_stays_read_only(tmp_config) -> None:
    """AUTOSDE.yaml backend-security-controls: the declared never-expiring grant
    gets no dashboard toggle, so the page shows it read-only and PATCH refuses it."""
    path = "agent.dangerously_skip_permissions"
    assert path not in all_settings.RISK_NOTE_PATHS
    assert all_settings.descriptor(_BY_PATH[path]) == {
        "mode": "readonly",
        "reason": all_settings.WHY_SAFETY,
    }
    assert all_settings.generic_spec(path) is None
    status, body = await _send({"path": path, "value": True})
    assert status == 400, body
    assert _stored_at(tmp_config, path) is None


@pytest.mark.parametrize(
    "path", ["agent.sandbox_allow_unsandboxed_exec", "tunnel.enabled", "agent.jail"]
)
async def test_an_app_token_cannot_write_a_risk_key(tmp_config, path) -> None:
    """The write stays owner-only: a caller carrying an app claim is refused."""
    value: object = "off" if path == "agent.jail" else True
    status, body = await _send({"path": path, "value": value}, identity=_app_token_identity)
    assert status in (401, 403), body
    assert _stored_at(tmp_config, path) is None


async def test_the_owner_saves_a_sandbox_read_only_folder(tmp_config, tmp_path) -> None:
    """An absolute folder on this host saves as written (built from tmp_path, so it
    is absolute on Windows too)."""
    folder = str(tmp_path / "shared")
    status, body = await _send({"path": "agent.sandbox_readonly_paths", "value": [folder]})
    assert status == 200, body
    assert _stored_at(tmp_config, "agent.sandbox_readonly_paths") == [folder]


@pytest.mark.parametrize("name", ["logo.svg", "LOGO.SVG"])
async def test_the_logo_file_is_never_an_svg(tmp_config, tmp_path, name) -> None:
    """The logo route serves this file to anyone who can open the sign-in page,
    and an SVG can carry script, so the page takes only raster images."""
    status, body = await _send({"path": "dashboard.avatar", "value": str(tmp_path / name)})
    assert status == 400, body
    assert _stored_at(tmp_config, "dashboard.avatar") is None


async def test_a_saved_risk_key_reads_back_through_the_loader(tmp_config) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    for path, value in (
        ("agent.deny_override", False),
        ("messaging.dm_scope", "unified"),
        ("agent.subagent_timeout_secs", 7200),
    ):
        status, body = await _send({"path": path, "value": value})
        assert status == 200, body
    cfg = KiroCrewConfig.load()
    assert cfg.agent.deny_override is False
    assert cfg.messaging.dm_scope == "unified"
    assert cfg.agent.subagent_timeout_secs == 7200
