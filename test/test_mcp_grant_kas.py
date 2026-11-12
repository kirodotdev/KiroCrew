"""The KAS grant vault's name-only presence check and the probe that combines it."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew import hooks, mcp_discovery_remote, mcp_grant_kas
from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.secrets import SecretVault
from kiro_crew.testing.links import make_dir_link

_FIGMA = "https://mcp.figma.com/mcp"
# sha256("https://mcp.figma.com/mcp|"): the input ``calculateConnectionHash`` in
# @kiro/agent's acp-server.js (KAS 2.36.0 and 2.37.1) builds for a url entry with
# no headers and no oauth, digested with node's crypto. A KAS upgrade that changes
# the engine's key format must update this on purpose, with the engine version.
_FIGMA_RECORDED_HASH = "4fa36849417c2230f5d4af2a1ae903f403562ebaddfdb8d2263efe9df7cfa71f"


@pytest.fixture(autouse=True)
def _persistence_on(_floor_monkeypatch):
    """Presence tests never consult the host's real config."""
    _floor_monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: True)


def test_the_connection_hash_matches_the_recorded_engine_value():
    assert mcp_grant_kas.kas_connection_hash(_FIGMA) == _FIGMA_RECORDED_HASH
    assert mcp_grant_kas.kas_tokens_entry_name(_FIGMA) == f"kiro.mcp.{_FIGMA_RECORDED_HASH}.tokens"


def test_the_connection_hash_trims_and_lowercases_like_the_engine():
    assert mcp_grant_kas.kas_connection_hash(" \tHTTPS://MCP.Figma.com/mcp\r\n") == (
        _FIGMA_RECORDED_HASH
    )


def test_the_connection_hash_keeps_what_javascript_trim_keeps():
    """Python's bare ``strip()`` also removes \\x1c-\\x1f; the engine's ``trim`` does not."""
    assert mcp_grant_kas.kas_connection_hash(f"{_FIGMA}\x1f") != _FIGMA_RECORDED_HASH


def test_the_store_and_the_check_share_one_layout(tmp_path):
    store = kss.McpGrantStore(tmp_path)
    assert store._root == mcp_grant_kas.grant_store_root(tmp_path)
    assert store._kas_dir == tmp_path / "kas"


async def _store_tokens(tmp_path, url: str) -> None:
    await kss.McpGrantStore(tmp_path).store(mcp_grant_kas.kas_tokens_entry_name(url), "{}")


@pytest.mark.asyncio
async def test_a_stored_tokens_entry_is_present(tmp_path):
    await _store_tokens(tmp_path, _FIGMA)
    root = mcp_grant_kas.grant_store_root(tmp_path)

    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is True
    assert mcp_grant_kas.kas_grant_presence("https://other.example/mcp", store_root=root) is False


@pytest.mark.asyncio
async def test_a_client_entry_alone_is_not_a_sign_in(tmp_path):
    name = f"kiro.mcp.{mcp_grant_kas.kas_connection_hash(_FIGMA)}.client"
    await kss.McpGrantStore(tmp_path).store(name, "{}")

    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is False


@pytest.mark.asyncio
async def test_the_check_never_reads_the_key_or_decrypts(tmp_path, monkeypatch):
    await _store_tokens(tmp_path, _FIGMA)

    def _refuse(*a: object, **kw: object) -> Any:
        raise AssertionError("the presence check touched the vault key")

    monkeypatch.setattr(SecretVault, "_get_or_create_key", _refuse)
    monkeypatch.setattr(SecretVault, "_decrypt_entry", _refuse)

    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is True


def test_no_vault_yet_is_a_definite_absence(tmp_path):
    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is False


def test_an_unparseable_envelope_is_unknowable(tmp_path):
    vault_dir = mcp_grant_kas.grant_store_root(tmp_path) / ".vault"
    vault_dir.mkdir(parents=True)
    (vault_dir / "secrets.enc").write_text("not json", encoding="utf-8")

    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is None


@pytest.mark.asyncio
async def test_an_envelope_that_cannot_be_stat_ed_is_unknowable(tmp_path, monkeypatch):
    """Not "absent": from Python 3.14 ``Path.exists()`` hides this error as False."""
    await _store_tokens(tmp_path, _FIGMA)
    envelope = mcp_grant_kas.grant_store_root(tmp_path) / ".vault" / "secrets.enc"
    real_stat = type(envelope).stat

    def _stat(self: Any, *a: Any, **kw: Any) -> Any:
        if self == envelope:
            raise PermissionError("EACCES")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(type(envelope), "stat", _stat)

    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is None


def test_deeply_nested_envelope_json_is_unknowable(tmp_path):
    vault_dir = mcp_grant_kas.grant_store_root(tmp_path) / ".vault"
    vault_dir.mkdir(parents=True)
    (vault_dir / "secrets.enc").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")

    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is None


def test_a_linked_grant_directory_is_unknowable(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "kas").mkdir()
    # A junction on Windows, so no symlink privilege is needed.
    make_dir_link(tmp_path / "kas" / "mcp-grants", elsewhere)

    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence(_FIGMA, store_root=root) is None


def test_a_non_ascii_url_is_unknowable(tmp_path):
    root = mcp_grant_kas.grant_store_root(tmp_path)
    assert mcp_grant_kas.kas_grant_presence("https://exämple.com/mcp", store_root=root) is None


def test_the_kas_presence_read_id_is_registered_for_audit():
    assert mcp_grant_kas._KAS_GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS


def _audit_calls(monkeypatch: pytest.MonkeyPatch, recorded: bool = True) -> list[Any]:
    seen: list[Any] = []

    def fake(read_id: str, outcome: str) -> bool:
        seen.append((read_id, outcome, threading.get_ident()))
        return recorded

    monkeypatch.setattr(mcp_grant_kas._hooks, "emit_internal_read_audit", fake)
    return seen


@pytest.mark.asyncio
async def test_persistence_off_skips_the_vault_and_audit(monkeypatch):
    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: False)
    presence = Mock(return_value=True)
    list_names = Mock()
    monkeypatch.setattr(mcp_grant_kas, "kas_grant_presence", presence)
    monkeypatch.setattr(SecretVault, "list_names", list_names)
    seen = _audit_calls(monkeypatch)

    assert await mcp_grant_kas.kas_grant_observed(_FIGMA) is False

    presence.assert_not_called()
    list_names.assert_not_called()
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_the_persistence_setting_is_resolved_off_the_loop(monkeypatch, enabled):
    threads: list[int] = []

    def persistence_enabled():
        threads.append(threading.get_ident())
        return enabled

    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", persistence_enabled)
    monkeypatch.setattr(mcp_grant_kas, "kas_grant_presence", lambda url: True)
    _audit_calls(monkeypatch)

    assert await mcp_grant_kas.kas_grant_observed(_FIGMA) is enabled
    assert len(threads) == 1
    assert threading.get_ident() not in threads


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_the_probe_ignores_stored_grants_when_persistence_is_off(
    tmp_path, monkeypatch, enabled
):
    await _store_tokens(tmp_path, _FIGMA)
    monkeypatch.setattr(mcp_grant_kas, "data_home", lambda: tmp_path)
    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: enabled)
    monkeypatch.setattr(mcp_discovery_remote, "grant_observed", AsyncMock(return_value=False))
    seen = _audit_calls(monkeypatch)

    assert await mcp_discovery_remote._runtime_grant_present(_FIGMA, "figma") is enabled
    assert [(rid, out) for rid, out, _ in seen] == (
        [(mcp_grant_kas._KAS_GRANT_PRESENCE_READ_ID, "success")] if enabled else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "outcome"), [(True, "success"), (False, "missing"), (None, "unreadable")]
)
async def test_every_answer_is_audited_off_the_loop(monkeypatch, answer, outcome):
    monkeypatch.setattr(mcp_grant_kas, "kas_grant_presence", lambda url: answer)
    seen = _audit_calls(monkeypatch)

    assert await mcp_grant_kas.kas_grant_observed(_FIGMA) is answer

    assert [(rid, out) for rid, out, _ in seen] == [
        (mcp_grant_kas._KAS_GRANT_PRESENCE_READ_ID, outcome)
    ]
    assert threading.get_ident() not in [thread for _, _, thread in seen]


@pytest.mark.asyncio
async def test_a_url_that_never_reaches_the_vault_is_not_audited(monkeypatch):
    seen = _audit_calls(monkeypatch)

    assert await mcp_grant_kas.kas_grant_observed("https://exämple.com/mcp") is None
    assert seen == []


@pytest.mark.asyncio
async def test_an_unrecordable_audit_logs_the_hash_never_the_url(monkeypatch, caplog):
    secret_url = "https://user:hunter2@mcp.example/mcp?token=abc"
    monkeypatch.setattr(mcp_grant_kas, "kas_grant_presence", lambda url: False)
    _audit_calls(monkeypatch, recorded=False)

    assert await mcp_grant_kas.kas_grant_observed(secret_url) is False

    assert mcp_grant_kas.kas_connection_hash(secret_url) in caplog.text
    assert "hunter2" not in caplog.text and "token=abc" not in caplog.text


def _patch_lookups(
    monkeypatch: pytest.MonkeyPatch, cli: bool | None, kas: bool | None
) -> tuple[AsyncMock, AsyncMock]:
    cli_mock = AsyncMock(return_value=cli)
    kas_mock = AsyncMock(return_value=kas)
    monkeypatch.setattr(mcp_discovery_remote, "grant_observed", cli_mock)
    monkeypatch.setattr(mcp_discovery_remote, "kas_grant_observed", kas_mock)
    return cli_mock, kas_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cli", "kas", "expected"),
    [
        (False, True, True),
        (None, True, True),
        (False, False, False),
        (None, False, None),
        (False, None, None),
        (None, None, None),
    ],
)
async def test_the_probe_combines_both_stores(monkeypatch, cli, kas, expected):
    _patch_lookups(monkeypatch, cli, kas)

    assert await mcp_discovery_remote._runtime_grant_present(_FIGMA, "figma") is expected


@pytest.mark.asyncio
async def test_a_kiro_cli_grant_decides_without_reading_the_vault(monkeypatch):
    _cli, kas_mock = _patch_lookups(monkeypatch, True, False)

    assert await mcp_discovery_remote._runtime_grant_present(_FIGMA, "figma") is True
    kas_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("cli", "expected"), [(False, None), (True, True)])
async def test_a_row_with_headers_skips_the_vault(monkeypatch, cli, expected):
    _cli, kas_mock = _patch_lookups(monkeypatch, cli, True)

    result = await mcp_discovery_remote._runtime_grant_present(_FIGMA, "figma", has_headers=True)

    assert result is expected
    kas_mock.assert_not_awaited()


def _challenging_session() -> Any:
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status = 401
    resp.headers = {"WWW-Authenticate": 'Bearer scope="openid"'}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(("headers", "expected"), [({}, True), ({"X-Org": "acme"}, None)])
async def test_the_probe_passes_whether_the_row_has_headers(monkeypatch, headers, expected):
    """A KAS-only grant reads "Signed in" for a plain row, "Not verified" with headers."""
    from unittest.mock import patch

    from kiro_crew.mcp_discovery import McpServerInfo, _probe_remote

    _patch_lookups(monkeypatch, False, True)
    server = McpServerInfo(name="figma", url=_FIGMA, headers=headers)
    with patch(
        "kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=_challenging_session()
    ):
        result = await _probe_remote(server)

    assert result.status == "needs_auth"
    assert result.auth_grant_present is expected
