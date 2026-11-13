"""The notice that an MCP server's sign-in cannot work on this backend stays in view.

A KAS session that drops a remote server's ``oauth`` block says so once, at session
start, in a ``notice`` row. Under the default collapse-all setting every untagged row
before the answer folds into the turn's steps, so the row carries
``meta.kind == "mcp_sign_in_unsupported"`` and the dashboard's turn fold exempts that
kind. Other lost spec keys keep the plain, foldable row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from kiro_crew.dashboard.chat_utils_injected_rows import MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND

_WEBSITE = Path(__file__).resolve().parents[1] / "website" / "src"


def _surfaced(monkeypatch, lost: list[str]) -> list[dict]:
    """The rows ``_prepare_spec_hooks`` surfaces on a new KAS session losing *lost*."""
    from kiro_crew.acp import kas_agents
    from kiro_crew.acp.types import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(spec_hooks, "_agent_spec_with_origin", lambda *_a, **_k: ({}, "user"))
    monkeypatch.setattr(kas_agents, "spec_keys_without_carrier", lambda _spec, **_kw: list(lost))
    rows: list[dict] = []

    def _capture(_state, _slot, role, content, cls="", *, meta=None, broadcast_user=False):
        rows.append({"role": role, "content": content, "meta": meta})
        return {}

    async def _nothing(*_a, **_kw):
        return None

    monkeypatch.setattr(chat_runner, "append_and_surface", _capture)
    # Only the lost-keys row is under test; the other session-start notices read
    # the filesystem and are covered by their own tests.
    monkeypatch.setattr(chat_runner, "_surface_overlong_hook_commands", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_withheld_project_hooks", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_withheld_project_hook_files", _nothing)
    client = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_KAS),
        cwd="",
        client=SimpleNamespace(initialize_result={"agentCapabilities": {}}),
    )
    asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), client, "kirocrew", is_new=True
        )
    )
    return [r for r in rows if "which this backend does not receive" in r["content"]]


def test_a_lost_oauth_block_tags_the_notice(monkeypatch) -> None:
    (row,) = _surfaced(monkeypatch, ["mcpServers.slack.oauth"])
    assert row["role"] == "notice"
    assert "Sign-in to MCP server slack will not work" in row["content"]
    assert row["meta"] == {"kind": MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND}


def test_other_lost_keys_keep_the_plain_row(monkeypatch) -> None:
    (row,) = _surfaced(monkeypatch, ["permissions"])
    assert row["meta"] is None


def test_the_website_keeps_the_kind_out_of_the_turn_fold() -> None:
    """A wire token: nothing links the two sides but the string."""
    source = (_WEBSITE / "pages" / "chat" / "TurnBlock.tsx").read_text(encoding="utf-8")
    predicate = source.split("const isAlwaysVisible", 1)[1].split("\n)\n", 1)[0]
    assert f"it.msg.meta?.kind === '{MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND}'" in predicate


def test_the_row_flags_only_an_oauth_block_no_engine_receives() -> None:
    """The MCP row carries ``oauthWithheld`` for a block Crew never sends to KAS."""
    from kiro_crew.mcp_discovery import _server_from_spec

    def row(oauth):
        spec = {"url": "https://mcp.example.com/mcp", "oauth": oauth}
        return _server_from_spec("s", spec, "mcp.json").to_dict()

    assert row({"clientId": "123.456", "clientSecret": "s3cret"})["oauthWithheld"] is True
    assert row({"clientMetadataUrl": "https://example.com/meta"})["oauthWithheld"] is True
    assert "oauthWithheld" not in row({"clientId": "123.456"})
    assert "oauthWithheld" not in row(None)
    assert "s3cret" not in repr(row({"clientId": "1", "clientSecret": "s3cret"}))
