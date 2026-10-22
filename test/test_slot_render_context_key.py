"""A row cache shares the string cache's context and keeps link restoration distinct."""

import json

import pytest

from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard import wire_render
from kiro_crew.security import exfil


@pytest.fixture(autouse=True)
def _clear_caches():
    cu._clear_display_redaction_cache()
    wire_render.clear_render_memo()
    yield
    cu._clear_display_redaction_cache()
    wire_render.clear_render_memo()


def test_row_context_follows_the_string_cache_context(monkeypatch):
    """A new string-cache dependency must also invalidate an already filled row."""
    version = {"value": 0}
    original_key = cu._display_redaction_cache_key
    original_render = cu._prepare_messages_scoped
    calls = []

    def key(text, *base, **snapshot):
        (digest, length), size = original_key(text, *base, **snapshot)
        return (digest + bytes([version["value"]]), length), size

    def render(*args, **kwargs):
        calls.append(version["value"])
        return original_render(*args, **kwargs)

    monkeypatch.setattr(cu, "_display_redaction_cache_key", key)
    monkeypatch.setattr(cu, "_prepare_messages_scoped", render)
    rows = [{"role": "assistant", "content": "unchanged"}]
    wire_render.prepare_messages_json(rows, False, live_child="")
    version["value"] = 1
    wire_render.prepare_messages_json(rows, False, live_child="")
    assert calls == [0, 1]


def test_policy_exemption_does_not_hide_workspace_restore_change(monkeypatch):
    """The union of policy and workspace hosts does not identify restoration scope."""
    host = "tenant.example.com"
    scoped = {"hosts": frozenset()}
    monkeypatch.setattr(cu, "_exempt_exact_hosts", lambda: frozenset({host}))
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: frozenset({host}))
    monkeypatch.setattr(cu, "allowed_hosts_for", lambda _: scoped["hosts"])
    url = f"https://{host}/view?q=hello"
    rows = [{"role": "assistant", "content": "blocked", "meta": {"marker": 1}}]
    original = cu.with_allowed_links_restored

    def restored(row):
        if row.get("meta", {}).get("marker") and host in cu.current_scoped_exempt_hosts():
            return {**row, "content": url}
        return original(row)

    # Isolate the documented restoration dependency from blocked-link record parsing.
    monkeypatch.setattr(cu, "with_allowed_links_restored", restored)
    first = wire_render.prepare_messages_json(rows, False, live_child="", workspace="/ws")
    scoped["hosts"] = frozenset({host})
    expected = json.dumps(cu._prepare_messages(rows, False, live_child="", workspace="/ws"))
    assert expected != first
    assert (
        wire_render.prepare_messages_json(rows, False, live_child="", workspace="/ws") == expected
    )


def test_oauth_endpoint_revocation_is_checked_on_every_read(monkeypatch):
    approved = {"value": True}
    monkeypatch.setattr(cu, "oauth_url_contains_credential", lambda _: not approved["value"])
    url = "https://auth.example.com/authorize?state=opaque"
    rows = [{"role": "mcp_oauth", "content": "", "meta": {"oauth_url": url, "child": "c"}}]
    first = wire_render.prepare_messages_json(rows, False, live_child="c")
    assert url in first
    approved["value"] = False
    expected = json.dumps(cu._prepare_messages(rows, False, live_child="c"))
    assert url not in expected
    assert wire_render.prepare_messages_json(rows, False, live_child="c") == expected
    assert wire_render.render_memo_info()[0] == 0
