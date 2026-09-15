"""Tests for per-method Slack pagination — cursor, legacy page/count, and none.

Covers the boundaries that are real bugs: history vs replies being INDEPENDENT
cursors, files.list being legacy (not cursor), search.* being legacy, and
usergroups.list refusing a paginator. Every pagination edge is exercised,
including the terminal signals and the misuse errors.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.slack.pagination import (
    CURSOR_LIMIT_MAX,
    CursorPaginator,
    PageCountPaginator,
    PaginationScheme,
    paginator_for,
    scheme_for,
)


@pytest.mark.parametrize(
    "method,expected",
    [
        ("conversations.history", PaginationScheme.CURSOR),
        ("conversations.replies", PaginationScheme.CURSOR),
        ("conversations.list", PaginationScheme.CURSOR),
        ("conversations.members", PaginationScheme.CURSOR),
        ("files.info", PaginationScheme.CURSOR),
        ("users.list", PaginationScheme.CURSOR),
        ("reactions.list", PaginationScheme.CURSOR),
        ("files.list", PaginationScheme.PAGE_COUNT),
        ("search.messages", PaginationScheme.PAGE_COUNT),
        ("search.files", PaginationScheme.PAGE_COUNT),
        ("search.all", PaginationScheme.PAGE_COUNT),
        ("usergroups.list", PaginationScheme.NONE),
    ],
)
def test_method_scheme_matches_reference(method, expected):
    assert scheme_for(method) is expected


def test_unknown_method_scheme_is_not_assumed():
    # The point of the module: a scheme must be declared, never defaulted.
    with pytest.raises(KeyError):
        scheme_for("chat.postMessage")


# --- cursor ------------------------------------------------------------------


def test_cursor_walk_advances_then_terminates_on_empty_cursor():
    p = paginator_for("conversations.history", limit=100)
    assert isinstance(p, CursorPaginator)
    assert p.next_params() == {"limit": 100}
    more = p.consume({"response_metadata": {"next_cursor": "abc="}})
    assert more is True
    assert p.next_params() == {"limit": 100, "cursor": "abc="}
    more = p.consume({"response_metadata": {"next_cursor": ""}})
    assert more is False
    assert p.done is True


def test_cursor_terminates_on_absent_response_metadata():
    p = paginator_for("users.list")
    assert p.consume({"members": [1, 2, 3]}) is False
    assert p.done is True


def test_cursor_fewer_than_limit_is_not_a_terminal_signal():
    # Slack: do NOT infer end from result count; only next_cursor is terminal.
    p = paginator_for("conversations.members", limit=1000)
    # One result but a next_cursor present -> MORE remains.
    more = p.consume({"members": ["U1"], "response_metadata": {"next_cursor": "z="}})
    assert more is True
    assert p.done is False


def test_next_params_after_done_raises():
    p = paginator_for("users.list")
    p.consume({"response_metadata": {"next_cursor": ""}})
    with pytest.raises(StopIteration):
        p.next_params()


def test_history_and_replies_are_independent_cursors():
    # Two SEPARATE walks; a cursor from one must never resume the other.
    hist = paginator_for("conversations.history")
    repl = paginator_for("conversations.replies")
    assert hist is not repl
    hist.consume({"response_metadata": {"next_cursor": "HIST="}})
    # replies is untouched by history's advance.
    assert repl.next_cursor is None
    assert repl.next_params() == {"limit": repl.limit}
    repl.consume({"response_metadata": {"next_cursor": "REPL="}})
    assert hist.next_cursor == "HIST="
    assert repl.next_cursor == "REPL="


@pytest.mark.parametrize("bad_limit", [0, -1, CURSOR_LIMIT_MAX + 1])
def test_cursor_limit_out_of_range_rejected(bad_limit):
    with pytest.raises(ValueError):
        CursorPaginator(method="users.list", limit=bad_limit)


# --- legacy page/count -------------------------------------------------------


def test_page_count_walk_advances_by_page_until_last():
    p = paginator_for("files.list", count=100)
    assert isinstance(p, PageCountPaginator)
    assert p.next_params() == {"page": 1, "count": 100}
    more = p.consume({"paging": {"count": 100, "total": 250, "page": 1, "pages": 3}})
    assert more is True
    assert p.total_pages == 3
    assert p.next_params() == {"page": 2, "count": 100}
    p.consume({"paging": {"count": 100, "total": 250, "page": 2, "pages": 3}})
    more = p.consume({"paging": {"count": 100, "total": 250, "page": 3, "pages": 3}})
    assert more is False
    assert p.done is True


def test_page_count_single_page_terminates_immediately():
    p = paginator_for("search.messages")
    more = p.consume({"messages": {"paging": {"count": 100, "total": 2, "page": 1, "pages": 1}}})
    assert more is False
    assert p.done is True


@pytest.mark.parametrize(
    "method,container_key",
    [
        ("search.messages", "messages"),
        ("search.files", "files"),
    ],
)
def test_search_page_count_reads_nested_paging(method, container_key):
    p = paginator_for(method)
    response = {container_key: {"paging": {"count": 100, "total": 250, "page": 1, "pages": 3}}}
    assert p.consume(response) is True
    assert p.next_params() == {"page": 2, "count": 100}


def test_search_all_keeps_walking_until_files_are_exhausted():
    p = paginator_for("search.all")
    first = {
        "messages": {"paging": {"page": 1, "pages": 1}},
        "files": {"paging": {"page": 1, "pages": 3}},
    }
    assert p.consume(first) is True
    assert p.next_params() == {"page": 2, "count": 100}

    second = {
        "messages": {},
        "files": {"paging": {"page": 2, "pages": 3}},
    }
    assert p.consume(second) is True
    assert p.next_params() == {"page": 3, "count": 100}

    third = {
        "messages": {"paging": {"page": 3, "pages": 99}},
        "files": {"paging": {"page": 3, "pages": 3}},
    }
    assert p.consume(third) is False
    assert p.done is True
    assert p.total_pages == 3


def test_search_all_other_container_walks_when_one_lacks_paging_on_first_response():
    # Fail-closed is PER CONTAINER: a container missing paging on its very first
    # response is marked done, and the other container still walks to completion.
    p = paginator_for("search.all")
    # messages has no paging from the start; files has three pages.
    assert p.consume({"messages": {}, "files": {"paging": {"page": 1, "pages": 3}}}) is True
    assert p.next_params() == {"page": 2, "count": 100}
    assert p.consume({"files": {"paging": {"page": 2, "pages": 3}}}) is True
    assert p.consume({"files": {"paging": {"page": 3, "pages": 3}}}) is False
    assert p.done is True


def test_search_all_is_done_only_when_both_containers_are_exhausted():
    p = paginator_for("search.all")
    assert p.consume(
        {
            "messages": {"paging": {"page": 1, "pages": 3}},
            "files": {"paging": {"page": 1, "pages": 1}},
        }
    )
    assert p.consume(
        {
            "messages": {"paging": {"page": 2, "pages": 3}},
            "files": {},
        }
    )
    assert (
        p.consume(
            {
                "messages": {"paging": {"page": 3, "pages": 3}},
                "files": {},
            }
        )
        is False
    )
    assert p.done is True


def test_page_count_missing_paging_fails_closed_to_done():
    # A response that does not describe its own paging must not loop forever.
    p = paginator_for("files.list")
    assert p.consume({"files": []}) is False
    assert p.done is True


def test_page_count_malformed_nested_paging_fails_closed():
    p = paginator_for("search.files")
    response = {
        "files": {"paging": {"page": "x", "pages": None}},
        "paging": {"page": 1, "pages": 3},
    }
    assert p.consume(response) is False
    assert p.done is True


# --- no pagination -----------------------------------------------------------


def test_usergroups_list_refuses_a_paginator():
    # usergroups.list returns the whole set in one call; asking for a paginator
    # is a caller bug, not something to paper over with a one-page paginator.
    with pytest.raises(ValueError):
        paginator_for("usergroups.list")
