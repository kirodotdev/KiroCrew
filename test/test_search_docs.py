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
    shipped = _json.loads((agent._BUNDLED_CFG_DIR / "defaults.json").read_text(encoding="utf-8"))
    # The shipped default template carries exactly the reviewed guide grants.
    assert [g for g in shipped["allowedTools"] if g.startswith("@kirocrew-guide")] == list(
        agent._GUIDE_AUTO_GRANTS
    )
    assert "@kirocrew-guide" in shipped["tools"]


def test_search_docs_reads_each_page_once_until_it_changes(tmp_path, monkeypatch):
    """A query does not re-read every packaged page; an edited page is read again."""
    from pathlib import Path

    (tmp_path / "alpha.md").write_text("# Alpha\nconnect slack here\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("# Beta\nnothing\n", encoding="utf-8")
    monkeypatch.setattr(mcp_guide, "_DOCS_DIR", tmp_path)
    mcp_guide._read_doc.cache_clear()
    reads: list[str] = []
    real = Path.read_text

    def counting(self, *a, **k):
        reads.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    assert [r["page"] for r in mcp_guide.search_docs("slack")["results"]] == ["alpha"]
    assert sorted(reads) == ["alpha.md", "beta.md"]
    reads.clear()
    mcp_guide.search_docs("slack")
    assert reads == []
    (tmp_path / "beta.md").write_text(
        "# Beta\nslack and more slack, now longer\n", encoding="utf-8"
    )
    assert {r["page"] for r in mcp_guide.search_docs("slack")["results"]} == {"alpha", "beta"}
    assert reads == ["beta.md"]


@pytest.mark.parametrize(
    ("query", "top"),
    [
        ("how do I set up a one-time reminder", "cron-and-scheduling"),
        ("how do I connect Slack", "slack-integration"),
        ("what do I do when a command is blocked", "blocked-commands"),
        ("how do I back up and restore my crew", "snapshot-and-restore"),
        ("how do I run a task in the background", "subagents"),
        ("how do I add a skill", "skills"),
        ("how do I use artifacts", "artifacts"),
    ],
)
def test_search_docs_ranks_the_page_a_how_to_question_is_about_first(query, top):
    assert mcp_guide.search_docs(query)["results"][0]["page"] == top


@pytest.mark.parametrize(
    ("query", "top"),
    [("reminder", "cron-and-scheduling"), ("telegram", "telegram-integration")],
)
def test_search_docs_one_word_query_still_finds_its_page(query, top):
    assert mcp_guide.search_docs(query)["results"][0]["page"] == top


def _docs(tmp_path, monkeypatch, pages: dict[str, str]) -> None:
    for name, body in pages.items():
        (tmp_path / f"{name}.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(mcp_guide, "_DOCS_DIR", tmp_path)
    mcp_guide._read_doc.cache_clear()


def test_search_docs_matches_whole_words_from_their_start(tmp_path, monkeypatch):
    """``do`` is not found inside ``window``; ``reminder`` finds ``reminders``."""
    _docs(
        tmp_path,
        monkeypatch,
        {
            "alpha": "# Alpha\nopen the window\n",
            "beta": "# Beta\nset reminders here\n",
            "gamma": "# Gamma\nraise `max_cycles`\n",
        },
    )
    assert mcp_guide.search_docs("do")["results"] == []
    assert [r["page"] for r in mcp_guide.search_docs("reminder")["results"]] == ["beta"]
    # ``_`` starts a word, so a part of a snake_case key still matches.
    assert [r["page"] for r in mcp_guide.search_docs("cycles")["results"]] == ["gamma"]


def test_search_docs_ranks_the_word_itself_above_the_start_of_a_longer_word(tmp_path, monkeypatch):
    """``mode`` prefers a page saying "mode" over one that only says "model"."""
    _docs(
        tmp_path,
        monkeypatch,
        {
            "alpha": "# Alpha\npick a model; the model; one more model\n",
            "beta": "# Beta\nturn on approval mode\n",
            "gamma": "# Gamma\nnothing here\n",
        },
    )
    assert [r["page"] for r in mcp_guide.search_docs("mode")["results"]] == ["beta", "alpha"]


@pytest.mark.parametrize(
    ("query", "page_text"),
    [("updates", "update it"), ("automatic", "runs automatically"), ("modes", "two modes")],
)
def test_search_docs_counts_a_common_ending_as_the_same_word(
    tmp_path, monkeypatch, query, page_text
):
    _docs(
        tmp_path,
        monkeypatch,
        {"alpha": f"# Alpha\n{page_text}\n", "beta": f"# Beta\n{query}tional {query}tional\n"},
    )
    assert mcp_guide.search_docs(query)["results"][0]["page"] == "alpha"


def test_search_docs_breaks_a_tie_toward_the_shorter_page_name(tmp_path, monkeypatch):
    _docs(tmp_path, monkeypatch, {"aa-long": "# A\nslack\n", "zz": "# Z\nslack\n"})
    assert [r["page"] for r in mcp_guide.search_docs("slack")["results"]] == ["zz", "aa-long"]


@pytest.mark.parametrize(
    ("query", "page_text"),
    [
        ("providers", "pick a provider"),
        ("provider", "all providers"),
        ("replies", "one reply"),
        ("reply", "two replies"),
        ("policies", "a policy"),
        ("policy", "the policies"),
    ],
)
def test_search_docs_plural_and_singular_find_each_other(tmp_path, monkeypatch, query, page_text):
    _docs(tmp_path, monkeypatch, {"alpha": f"# Alpha\n{page_text}\n", "beta": "# Beta\nother\n"})
    assert [r["page"] for r in mcp_guide.search_docs(query)["results"]] == ["alpha"]


def test_search_docs_drops_question_words_unless_nothing_else_is_left(tmp_path, monkeypatch):
    _docs(
        tmp_path,
        monkeypatch,
        {"alpha": "# Alpha\nhow do I do this, how, how\n", "beta": "# Beta\nslack\n"},
    )
    assert [r["page"] for r in mcp_guide.search_docs("how do I use slack")["results"]] == ["beta"]
    # Only question words: they are searched rather than refused.
    assert [r["page"] for r in mcp_guide.search_docs("how")["results"]] == ["alpha"]
