"""A global mcp.json that does not parse is refused, never replaced.

``PUT /api/mcp/servers/{name}`` and ``POST /api/mcp/sync`` read the Kiro-global
``mcp.json``, add a server and write the document back. Folding a parse error
into the missing-file fallback replaced the user's whole file with a document
holding only the new server. Each writer now answers ``500 config_corrupt``, as
the toggle route does, and the file keeps its exact bytes.

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _read_global, _request, _write_global

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo

# The reproduction from the issue: one missing comma between two entries.
_BROKEN = '{"mcpServers": {"usera": {"command": "a"} "userb": {"command": "b"}}}'


@pytest.fixture(autouse=True)
def _owned_home(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent home is ours, so each route reaches its mcp.json step."""
    _floor_monkeypatch.setattr(mcp_mod, "_agent_home_not_owned", lambda _name: None)


def _put(name: str = "newsrv") -> Any:
    return mcp_mod.api_mcp_server_detail(
        _request(
            {"command": "node", "args": ["s.js"]},
            match_info={"name": name},
            method="PUT",
        )
    )


@pytest.mark.asyncio
async def test_put_refuses_an_unparseable_file_and_leaves_its_bytes(
    sandbox: SimpleNamespace,
) -> None:
    sandbox.global_json.write_text(_BROKEN, encoding="utf-8")

    resp = await _put()

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert sandbox.global_json.read_text(encoding="utf-8") == _BROKEN
    assert sandbox.synced == []


@pytest.mark.asyncio
async def test_put_still_creates_a_missing_file(sandbox: SimpleNamespace) -> None:
    assert not sandbox.global_json.exists()

    resp = await _put()

    assert resp.status == 200, _payload(resp)
    assert set(_read_global(sandbox)) == {"newsrv"}


@pytest.mark.asyncio
async def test_put_keeps_the_other_servers_of_a_valid_file(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {"usera": {"command": "a"}, "userb": {"command": "b"}})

    resp = await _put()

    assert resp.status == 200, _payload(resp)
    assert set(_read_global(sandbox)) == {"usera", "userb", "newsrv"}


@pytest.mark.asyncio
async def test_sync_refuses_an_unparseable_file_before_any_write(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.mcp_discovery as disc

    calls: list[None] = []

    def _discover() -> list[McpServerInfo]:
        calls.append(None)
        return [McpServerInfo(name="found", command="found-cmd")]

    monkeypatch.setattr(disc, "sync_discovered_servers", _discover)
    sandbox.global_json.write_text(_BROKEN, encoding="utf-8")

    resp = await mcp_mod.api_mcp_sync(_request(None))

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert calls == [], "the discover pass writes the agent config; it must not run"
    assert sandbox.global_json.read_text(encoding="utf-8") == _BROKEN


@pytest.mark.asyncio
async def test_sync_refuses_a_file_that_breaks_after_the_first_check(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read under the mcp lock refuses too: the early check is not the only gate.

    The discover pass has already run by then, so this pins only that the global
    file survives; the request is refused whole only when the file is broken at
    request start (the tests above).
    """
    import kiro_crew.mcp_discovery as disc

    def _discover() -> list[McpServerInfo]:
        sandbox.global_json.write_text(_BROKEN, encoding="utf-8")
        return [McpServerInfo(name="found", command="found-cmd")]

    monkeypatch.setattr(disc, "sync_discovered_servers", _discover)
    monkeypatch.setattr(disc, "kirocrew_managed_names", set)
    _write_global(sandbox, {"usera": {"command": "a"}})

    resp = await mcp_mod.api_mcp_sync(_request(None))

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert sandbox.global_json.read_text(encoding="utf-8") == _BROKEN
    assert sandbox.batched == []


# Each shape a writer must not read as an empty document: a parse error, bytes
# that are not UTF-8, a root that is not an object, an ``mcpServers`` that is not
# an object.
_NOT_A_CONFIG: list[tuple[str, bytes]] = [
    ("syntax", _BROKEN.encode()),
    ("not-utf8", b'{"mcpServers": {"usera": {"command": "\xff"}}}'),
    ("list-root", b"[]"),
    ("null-root", b"null"),
    ("list-servers", b'{"mcpServers": []}'),
    ("null-servers", b'{"mcpServers": null}'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [p[1] for p in _NOT_A_CONFIG], ids=[p[0] for p in _NOT_A_CONFIG])
async def test_put_refuses_every_non_config_shape(sandbox: SimpleNamespace, raw: bytes) -> None:
    sandbox.global_json.write_bytes(raw)

    resp = await _put()

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert sandbox.global_json.read_bytes() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [p[1] for p in _NOT_A_CONFIG], ids=[p[0] for p in _NOT_A_CONFIG])
async def test_sync_refuses_every_non_config_shape_before_discovery(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    import kiro_crew.mcp_discovery as disc

    calls: list[None] = []
    monkeypatch.setattr(disc, "sync_discovered_servers", lambda: calls.append(None) or [])
    sandbox.global_json.write_bytes(raw)

    resp = await mcp_mod.api_mcp_sync(_request(None))

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert calls == []
    assert sandbox.global_json.read_bytes() == raw


@pytest.mark.asyncio
async def test_apply_refuses_an_unparseable_global_before_any_write(
    sandbox: SimpleNamespace,
) -> None:
    sandbox.global_json.write_text(_BROKEN, encoding="utf-8")

    resp = await mcp_mod.api_mcp_apply(
        _request({"changes": [{"name": "newsrv", "kirocrew": True, "kiroGlobal": True}]})
    )

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert sandbox.global_json.read_text(encoding="utf-8") == _BROKEN
    assert sandbox.synced == [] and sandbox.batched == []


@pytest.mark.parametrize("raw", [p[1] for p in _NOT_A_CONFIG], ids=[p[0] for p in _NOT_A_CONFIG])
def test_scope_writer_returns_corrupt_and_writes_nothing(
    sandbox: SimpleNamespace, raw: bytes
) -> None:
    """The apply route's per-scope writer refuses too, for a file that breaks mid-batch."""
    sandbox.global_json.write_bytes(raw)

    action = mcp_mod._set_scope_entry(
        sandbox.global_json, "newsrv", enabled=True, spec={"command": "node"}
    )

    assert action == "corrupt"
    assert sandbox.global_json.read_bytes() == raw


@pytest.mark.asyncio
async def test_apply_refuses_an_unparseable_extra_scope_global(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check covers every provider global file, not only the Kiro one."""
    other = sandbox.global_json.parent / "other-provider.json"
    other.write_text(_BROKEN, encoding="utf-8")
    monkeypatch.setattr(
        mcp_mod, "_extra_mcp_scopes", lambda: [SimpleNamespace(id="other", global_json=other)]
    )
    _write_global(sandbox, {"usera": {"command": "a"}})
    before = sandbox.global_json.read_bytes()

    resp = await mcp_mod.api_mcp_apply(
        _request({"changes": [{"name": "newsrv", "kirocrew": True, "kiroGlobal": True}]})
    )

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert other.read_text(encoding="utf-8") == _BROKEN
    assert sandbox.global_json.read_bytes() == before


@pytest.mark.asyncio
async def test_put_refuses_a_file_that_cannot_be_read(sandbox: SimpleNamespace) -> None:
    sandbox.global_json.mkdir()

    resp = await _put()

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert sandbox.global_json.is_dir()


@pytest.mark.asyncio
async def test_put_still_accepts_a_bom_prefixed_file(sandbox: SimpleNamespace) -> None:
    sandbox.global_json.write_bytes(
        b"\xef\xbb\xbf" + b'{"mcpServers": {"usera": {"command": "a"}}}'
    )

    resp = await _put()

    assert resp.status == 200, _payload(resp)
    assert set(_read_global(sandbox)) == {"usera", "newsrv"}


@pytest.mark.asyncio
async def test_put_keeps_the_commented_file_refusal(sandbox: SimpleNamespace) -> None:
    """A JSONC file parses, so it reaches the 409 comments refusal, not config_corrupt."""
    commented = '{\n  // mine\n  "mcpServers": {"usera": {"command": "a"}}\n}\n'
    sandbox.global_json.write_text(commented, encoding="utf-8")

    with pytest.raises(mcp_mod.McpConfigHasComments):
        await _put()

    assert sandbox.global_json.read_text(encoding="utf-8") == commented


@pytest.mark.parametrize("raw", [p[1] for p in _NOT_A_CONFIG], ids=[p[0] for p in _NOT_A_CONFIG])
def test_cc_sidecar_is_not_replaced(tmp_path, monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    """The sync's Claude Code sidecar write refuses the same shapes."""
    from kiro_crew import mcp_discovery

    monkeypatch.setattr(mcp_discovery, "kirocrew_managed_names", set)
    sidecar = tmp_path / ".mcp.json"
    sidecar.write_bytes(raw)
    server = McpServerInfo(name="found", command="found-cmd")

    assert mcp_discovery.register_servers_for_cc([server], mcp_json_path=sidecar) is False
    assert sidecar.read_bytes() == raw


def test_cc_sidecar_is_still_created_when_missing(tmp_path, monkeypatch) -> None:
    from kiro_crew import mcp_discovery

    monkeypatch.setattr(mcp_discovery, "kirocrew_managed_names", set)
    sidecar = tmp_path / ".mcp.json"
    server = McpServerInfo(name="found", command="found-cmd")

    assert mcp_discovery.register_servers_for_cc([server], mcp_json_path=sidecar) is True
    assert "found" in json.loads(sidecar.read_text(encoding="utf-8"))["mcpServers"]


@pytest.mark.asyncio
async def test_apply_refuses_when_a_global_breaks_mid_batch(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that breaks after the pre-check stops the batch with config_corrupt, not 200."""
    _write_global(sandbox, {"usera": {"command": "a"}})
    real = mcp_mod._set_scope_entry
    calls: list[str] = []

    def _break_then_write(path, name, **kwargs):
        calls.append(name)
        path.write_text(_BROKEN, encoding="utf-8")
        return real(path, name, **kwargs)

    monkeypatch.setattr(mcp_mod, "_set_scope_entry", _break_then_write)

    resp = await mcp_mod.api_mcp_apply(
        _request(
            {
                "changes": [
                    {"name": "first", "kirocrew": True, "kiroGlobal": True},
                    {"name": "second", "kirocrew": True, "kiroGlobal": True},
                ]
            }
        )
    )

    assert resp.status == 500
    assert _payload(resp)["code"] == "config_corrupt"
    assert sandbox.global_json.read_text(encoding="utf-8") == _BROKEN
    assert calls == ["first"], "the batch stops at the change whose scope write was refused"
