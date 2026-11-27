"""Search all chats matches the redacted text its results show: the chat ranking
(``search_sessions``, scan and index alike), the ``title:`` scope, and the message
list (``message_hits``)."""

from __future__ import annotations

import json

import pytest

from kiro_crew import history_search
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.dashboard.handlers.sessions import message_hits, message_query
from kiro_crew.history import ConversationLog
from kiro_crew.testing.synthetic_credentials import SAMPLE_SLACK_BOT_TOKEN
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

TOKEN = SAMPLE_SLACK_BOT_TOKEN
PART = TOKEN[8:24]
ROLES = frozenset({"user", "assistant"})


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(history_search, "_INDEX_QUIET_WINDOW_SECS", 0.0)
    made = ConversationLog(base_dir=tmp_path)
    yield made
    index = getattr(made._catalog_projection, "_index", None)
    if index is not None:
        index.close()


def _write(log, key: str, contents: list[str], title: str = "") -> None:
    """Write a transcript as a dashboard save stores it: the text as typed."""
    head = {"_type": "metadata", "created_at": "2026-11-20T10:00:00+00:00", "title": title}
    rows = [
        {"role": "user", "content": text, "ts": f"2026-11-20T10:00:{i:02d}+00:00"}
        for i, text in enumerate(contents)
    ]
    path = log._path(key)
    path.write_text("\n".join(json.dumps(r) for r in [head, *rows]) + "\n", encoding="utf-8")
    log._invalidate_cache(key)


def _keys(rows) -> set[str]:
    return {row["key"] for row in rows}


def _corpus(log) -> None:
    _write(log, "dashboard:secret", [f"my token is {TOKEN}", "a plum"])
    _write(log, "dashboard:plain", ["the plum report"])
    _write(log, "dashboard:titled", ["nothing here"], title=f"notes {TOKEN}")


def test_the_ranking_matches_the_redacted_text_and_titles(log):
    _corpus(log)

    assert _keys(log.search_sessions(PART)) == set()
    assert _keys(log.search_sessions(f"title:{PART}")) == set()
    assert _keys(log.search_sessions("plum")) == {"dashboard_secret", "dashboard_plain"}


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
def test_the_index_holds_and_matches_the_redacted_text(log):
    _corpus(log)
    log._catalog_projection.backfill_index(budget_secs=FALLBACK_TIMEOUT_SECS)

    assert _keys(log.search_sessions(PART)) == set()
    assert _keys(log.search_sessions("plum")) == {"dashboard_secret", "dashboard_plain"}
    index = log._catalog_projection.search_index
    st = log._path("dashboard:secret").stat()
    stored = "\n".join(index.iter_raw_texts("dashboard_secret", st) or [])
    assert "plum" in stored and PART not in stored


def test_a_text_read_again_is_still_matched_in_its_redacted_form(log):
    _corpus(log)
    assert _keys(log.search_sessions("plum")) == {"dashboard_secret", "dashboard_plain"}
    # The transcript is read again (its fold memo dropped): the texts come back
    # from the redaction memo, a changed one still redacted.
    log._invalidate_cache("dashboard:secret")
    log._folded_cache.clear()

    assert _keys(log.search_sessions(PART)) == set()
    texts = list(log._iter_message_texts("dashboard_secret"))
    assert texts[1] == "a plum" and PART not in texts[0]


@pytest.fixture
def fresh_memo(monkeypatch):
    """The redaction memo (emptied per test by the conftest floor), and the
    published setting restored afterwards."""
    from kiro_crew import redaction_seams, search_shown_text

    monkeypatch.setattr(redaction_seams, "_redaction_off", False)
    monkeypatch.setattr(redaction_seams, "_generation", redaction_seams._generation)
    return search_shown_text


def test_changed_texts_are_returned_in_full_with_a_bounded_memo(fresh_memo, monkeypatch):
    from kiro_crew import redaction_seams

    texts = [f"{label} {TOKEN}" for label in "abc"]
    shown = [fresh_memo.redact(text) for text in texts]
    ceiling = len(shown[0])
    budget = 2 * ceiling
    monkeypatch.setattr(fresh_memo, "CHANGED_TEXT_MAX_CHARS", ceiling)
    monkeypatch.setattr(fresh_memo, "CHANGED_MEMO_CHARS", budget)

    oversized = f"{'x' * ceiling} {TOKEN}"
    expected = fresh_memo.redact(oversized)
    assert len(expected) > ceiling and PART not in expected
    assert fresh_memo.shown_text(oversized) == expected
    assert not fresh_memo._changed and fresh_memo._changed_chars == 0
    assert fresh_memo._digest(oversized) not in fresh_memo._clean

    for text, expected in zip(texts, shown):
        assert fresh_memo.shown_text(text) == expected
        assert PART not in expected
        assert fresh_memo._changed_chars == sum(
            len(value) for value in fresh_memo._changed.values()
        )
        assert fresh_memo._changed_chars <= budget
    assert list(fresh_memo._changed.values()) == shown[1:]
    assert fresh_memo.shown_text(texts[-1]) == shown[-1]
    assert fresh_memo._changed_chars == budget

    redaction_seams.publish(True)
    assert fresh_memo.shown_text("plain") == "plain"
    assert not fresh_memo._changed and fresh_memo._changed_chars == 0


def test_a_flip_of_the_redaction_setting_never_serves_the_old_form(fresh_memo, monkeypatch):
    from kiro_crew import redaction_seams

    seen: list[str] = []
    real = fresh_memo.redact

    def counted(text: str) -> str:
        seen.append(text)
        return real(text)

    monkeypatch.setattr(fresh_memo, "redact", counted)
    secret = f"my token is {TOKEN}"
    first = fresh_memo.shown_text(secret)
    assert PART not in first
    assert fresh_memo.shown_text(secret) == first and seen == [secret]

    # Off and back on: the memo answered under the old position is dropped.
    redaction_seams.publish(True)
    redaction_seams.publish(False)
    assert fresh_memo.shown_text(secret) == first
    assert seen == [secret, secret]


def test_a_change_of_the_exempt_url_prefixes_never_serves_the_old_form(fresh_memo):
    """The memo is keyed on the token-parameter prefixes the installed context
    exempts, so a form kept under one context is not served under another."""
    import dataclasses

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.context import set_context

    prefix = "https://approve.example.com/auth"
    value = "eyJ3b3JrZmxvd0lkIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAwIn0%3D"
    url = f"{prefix}?token={value}"

    class _Credentials:
        def __init__(self, prefixes: frozenset[str]):
            self._prefixes = prefixes

        def redact(self, text: str) -> str:
            return fresh_memo.redact(text)

        def exempt_exact_hosts(self) -> frozenset[str]:
            return frozenset({"approve.example.com"})

        def token_param_exempt_url_prefixes(self) -> frozenset[str]:
            return self._prefixes

    base = build_default_context(KiroCrewConfig())

    def install(prefixes: frozenset[str]) -> None:
        set_context(dataclasses.replace(base, credentials=_Credentials(prefixes)))

    install(frozenset({prefix}))
    assert fresh_memo.shown_text(url) == url
    install(frozenset())
    assert value not in fresh_memo.shown_text(url)
    install(frozenset({prefix}))
    assert fresh_memo.shown_text(url) == url


def test_a_caller_the_credential_pass_stands_down_for_never_fills_the_memo(fresh_memo):
    from kiro_crew.security.redaction_switch import owner_view

    secret = f"my token is {TOKEN}"
    with owner_view():
        assert fresh_memo.shown_text(secret) == secret

    assert PART not in fresh_memo.shown_text(secret)


def test_the_message_list_matches_the_redacted_text():
    rows = [
        {"role": "user", "content": f"my token is {TOKEN}"},
        {"role": "assistant", "content": "the plum report"},
        {"role": "user", "content": f"plum {TOKEN}"},
    ]

    assert message_hits(rows, message_query(PART), ROLES) == ([], 0)
    hits, count = message_hits(rows, message_query("plum"), ROLES)
    assert count == 2 and [h["index"] for h in hits] == [1, 2]
    # A word the redaction marker shows is found, as the excerpt shows it.
    hits, count = message_hits(rows, message_query("redacted"), ROLES)
    assert count == 2 and [h["index"] for h in hits] == [0, 2]
    # An excluded term only inside the redacted text excludes nothing.
    hits, count = message_hits(rows, message_query(f"plum -{TOKEN[-8:-2]}"), ROLES)
    assert count == 2
