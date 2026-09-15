"""Tests for the Asana pagination contract.

Covers the limit 1..100 bound (negative: out-of-range, wrong type), the offset
cursor as an opaque token (negative: numeric offset), next_page parsing and
exhaustion, offset-token expiry as an EXPIRABLE-but-unknown-TTL condition, and
the ~1000-object legacy non-paginated truncation ceiling.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.asana.pagination import (
    LEGACY_UNPAGINATED_TRUNCATION_LIMIT,
    LIMIT_MAX,
    LIMIT_MIN,
    AsanaPaginationError,
    OffsetTokenExpired,
    classify_offset_rejection,
    continue_request,
    has_more,
    legacy_unpaginated_truncated,
    normalize_page_request,
    parse_next_page,
)

# ── limit is bounded to 1..100 ──────────────────────────────────────────────


def test_limit_none_is_server_default():
    assert normalize_page_request().limit is None


def test_limit_at_min_and_max_accepted():
    assert normalize_page_request(limit=LIMIT_MIN).limit == 1
    assert normalize_page_request(limit=LIMIT_MAX).limit == 100


def test_limit_below_min_rejected():
    with pytest.raises(AsanaPaginationError):
        normalize_page_request(limit=0)


def test_limit_above_max_rejected():
    with pytest.raises(AsanaPaginationError):
        normalize_page_request(limit=101)


def test_limit_bool_rejected():
    # bool is an int subclass in Python; True must not slip through as limit=1.
    with pytest.raises(AsanaPaginationError):
        normalize_page_request(limit=True)


def test_limit_non_int_rejected():
    with pytest.raises(AsanaPaginationError):
        normalize_page_request(limit="50")


# ── offset is an opaque cursor token, never a number ────────────────────────


def test_offset_none_is_first_page():
    assert normalize_page_request().offset is None


def test_offset_opaque_token_accepted():
    req = normalize_page_request(offset="eyJvZmZzZXQiOjUwfQ")
    assert req.offset == "eyJvZmZzZXQiOjUwfQ"


def test_offset_numeric_int_rejected():
    # Treating the offset as a row index (an int) is a shaping error.
    with pytest.raises(AsanaPaginationError):
        normalize_page_request(offset=50)


def test_offset_empty_string_rejected():
    with pytest.raises(AsanaPaginationError):
        normalize_page_request(offset="   ")


# ── next_page parsing and exhaustion ────────────────────────────────────────


def test_parse_next_page_reads_cursor():
    cursor = parse_next_page({"data": [], "next_page": {"offset": "tok", "path": "/p", "uri": "u"}})
    assert cursor is not None
    assert cursor.offset == "tok"
    assert cursor.path == "/p"


def test_parse_next_page_null_is_exhausted():
    assert parse_next_page({"data": [], "next_page": None}) is None


def test_parse_next_page_absent_is_exhausted():
    assert parse_next_page({"data": []}) is None


def test_parse_next_page_present_without_offset_is_malformed():
    with pytest.raises(AsanaPaginationError):
        parse_next_page({"next_page": {"path": "/p"}})


def test_has_more_true_when_cursor_present():
    assert has_more({"next_page": {"offset": "tok"}}) is True


def test_has_more_false_when_exhausted():
    assert has_more({"next_page": None}) is False


def test_continue_request_carries_limit_and_adopts_server_offset():
    base = normalize_page_request(limit=100)
    nxt = continue_request(base, {"next_page": {"offset": "server-tok"}})
    assert nxt is not None
    assert nxt.limit == 100
    assert nxt.offset == "server-tok"


def test_continue_request_none_when_exhausted():
    base = normalize_page_request(limit=100)
    assert continue_request(base, {"next_page": None}) is None


def test_full_page_with_null_next_page_is_last_page():
    # A page exactly `limit` long but with a null next_page is the LAST page;
    # continuation is decided by next_page, never by page fullness.
    base = normalize_page_request(limit=2)
    assert continue_request(base, {"data": [{}, {}], "next_page": None}) is None


# ── offset token is expirable, TTL unknown ──────────────────────────────────


def test_classify_offset_rejection_yields_typed_expiry():
    err = classify_offset_rejection(stale=True, detail="server said expired")
    assert isinstance(err, OffsetTokenExpired)
    assert "restart" in str(err).lower()


def test_classify_offset_rejection_refuses_non_stale():
    # The function models EXPIRY only; a non-stale rejection is a misuse.
    with pytest.raises(AsanaPaginationError):
        classify_offset_rejection(stale=False)


def test_offset_expiry_carries_no_http_status_assertion():
    # The condition must not pin a 403-vs-404 (or any) status: TTL and the
    # rejection code are undocumented. OffsetTokenExpired has no status field.
    err = classify_offset_rejection(stale=True)
    assert not hasattr(err, "status")


# ── legacy non-paginated ~1000 truncation ceiling ───────────────────────────


def test_legacy_unpaginated_below_ceiling_is_complete():
    assert legacy_unpaginated_truncated(999) is False


def test_legacy_unpaginated_at_ceiling_is_possibly_truncated():
    assert legacy_unpaginated_truncated(LEGACY_UNPAGINATED_TRUNCATION_LIMIT) is True


def test_legacy_unpaginated_above_ceiling_is_truncated():
    assert legacy_unpaginated_truncated(LEGACY_UNPAGINATED_TRUNCATION_LIMIT + 500) is True


def test_legacy_unpaginated_negative_count_rejected():
    with pytest.raises(AsanaPaginationError):
        legacy_unpaginated_truncated(-1)
