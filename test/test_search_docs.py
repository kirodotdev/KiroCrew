"""``search_docs``: a read-only search over the packaged ``kiro_crew/docs`` pages."""

from __future__ import annotations

import json

import pytest

from kiro_crew import mcp_guide


def test_search_docs_finds_slack_setup():
    d = mcp_guide.search_docs("connect Slack")
    pages = [r["page"] for r in d["results"]]
    assert "slack-integration" in pages
    assert len(pages) <= 5


def test_search_docs_reads_a_page_and_pages_on():
    d = mcp_guide.search_docs(page="slack-integration")
    assert d["page"] == "slack-integration" and d["text"]
    big = max(mcp_guide._doc_pages().items(), key=lambda kv: kv[1].stat().st_size)
    first = mcp_guide.search_docs(page=big[0])
    if "next_offset" in first:
        more = mcp_guide.search_docs(page=big[0], offset=first["next_offset"])
        assert more["text"] and more["text"] != first["text"]


@pytest.mark.parametrize(
    "page",
    [
        "../agent",
        "/etc/passwd",
        "../../pyproject",
        "README/../../x",
        "nope",
        # Real .md files OUTSIDE the docs directory in a source checkout.
        "../../../README",
        "../../../docs/system-specs/modules/crew-mode",
    ],
)
def test_search_docs_never_reads_outside_the_docs_listing(page):
    assert "error" in mcp_guide.search_docs(page=page)


def test_search_docs_finds_where_closed_chats_live():
    d = mcp_guide.search_docs("closed sessions older sessions")
    blob = json.dumps(d).lower()
    assert "older sessions" in blob


def test_search_docs_needs_no_caller_identity(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("search_docs must not need a session key or a gateway call")

    monkeypatch.setattr(mcp_guide, "_strict_session_key", boom)
    monkeypatch.setattr(mcp_guide, "_get", boom)
    monkeypatch.setattr(mcp_guide, "_post", boom)
    out = json.loads(mcp_guide._call_tool_inner("search_docs", {"query": "Slack"}))
    assert out["results"]


def test_search_docs_and_find_ui_are_pre_approved_reads_on_every_agent():
    import json as _json

    from kiro_crew import agent

    assert "@kirocrew-guide/search_docs" in agent._GUIDE_AUTO_GRANTS
    assert "@kirocrew-guide/find_ui" in agent._GUIDE_AUTO_GRANTS
    shipped = _json.loads((agent._BUNDLED_CFG_DIR / "defaults.json").read_text())
    # The shipped default template carries exactly the reviewed guide grants.
    assert [g for g in shipped["allowedTools"] if g.startswith("@kirocrew-guide")] == list(
        agent._GUIDE_AUTO_GRANTS
    )
    assert "@kirocrew-guide" in shipped["tools"]
