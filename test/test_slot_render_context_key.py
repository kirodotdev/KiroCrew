"""A row cache shares the string cache's context, and never memoizes an OAuth row."""

import json

import pytest

from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard import mcp_oauth_provenance, wire_render


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


def test_a_policy_exemption_does_not_hide_a_workspace_allowance(monkeypatch):
    """The platform's exempt hosts and the workspace's allowed hosts are keyed apart.

    The same host moving from one to the other draws no card either way, yet the
    second render must miss the memo: only an allowance's use is audited.
    """
    from kiro_crew.security import blocked_links, exfil

    host = "tenant.example.com"
    url = f"https://{host}/c?d=" + "x" * 260
    policy = {"hosts": frozenset({host})}
    allowed = {"hosts": frozenset()}
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: policy["hosts"])
    monkeypatch.setattr(cu, "allowed_hosts_for", lambda _ws: allowed["hosts"])
    audited: list[str] = []
    monkeypatch.setattr(
        blocked_links, "_emit_allowed_host_used_event", lambda h, _rules: audited.append(h)
    )
    rows = [{"role": "assistant", "content": url, "ts": "1"}]
    first = wire_render.prepare_messages_json(rows, False, live_child="", workspace="/ws")
    policy["hosts"] = frozenset()
    allowed["hosts"] = frozenset({host})
    second = wire_render.prepare_messages_json(rows, False, live_child="", workspace="/ws")
    assert first == second
    assert cu.BLOCKED_LINKS_KEY not in second
    assert audited == [host]


def test_oauth_endpoint_revocation_is_checked_on_every_read(monkeypatch):
    approved = {"value": True}
    monkeypatch.setattr(cu, "oauth_url_contains_credential", lambda _: not approved["value"])
    url = "https://auth.example.com/authorize?state=opaque"
    # A banner child "c" sent, so the endpoint approval is what decides the link.
    mcp_oauth_provenance.record_emitted("c", url)
    rows = [{"role": "mcp_oauth", "content": "", "meta": {"oauth_url": url, "child": "c"}}]
    first = wire_render.prepare_messages_json(rows, False, live_child="c")
    assert url in first
    approved["value"] = False
    expected = json.dumps(cu._prepare_messages(rows, False, live_child="c"))
    assert url not in expected
    assert wire_render.prepare_messages_json(rows, False, live_child="c") == expected
    assert wire_render.render_memo_info()[0] == 0
