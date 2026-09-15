"""Tests for the Salesforce vendor-offline core (W10 / L1).

Covers the positive contract and, emphatically, the negative paths: malformed
describe, mistyped payloads, pagination-contract violations, unrecognized bulk
states, results-before-downloadable, anonymous-Apex rejection, and the FLS /
object-permission separation. All fixtures are offline and stamped as such.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.salesforce import (
    UNKNOWN,
    ApexCapabilityError,
    BulkStateError,
    IdempotencyClass,
    PayloadParseError,
    QueryPaginationError,
    SalesforceErrorCategory,
    classify_status,
    classify_vendor_error_code,
    fixtures,
    idempotency_class_for,
    next_locator,
    parse_ingest_job_status,
    parse_object_describe,
    parse_query_page,
    parse_record,
    parse_typed_field,
    partition_row_outcomes,
    register_authorized_rest_resource,
    reject_execute_anonymous,
    results_downloadable,
)
from kiro_crew.connections.vendors.salesforce.describe import SourceKind
from kiro_crew.connections.vendors.salesforce.idempotency import (
    IDEMPOTENCY_CLASSES,
    is_salesforce_applicable,
)

# --------------------------------------------------------------------------- #
# describe: FLS and object permissions are separate axes
# --------------------------------------------------------------------------- #


def test_describe_keeps_fls_and_object_permissions_distinct() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    # object-level permissions
    assert obj.object_permissions.createable is True
    assert obj.object_permissions.queryable is True
    assert obj.object_permissions.deletable is False
    # per-field FLS is a DIFFERENT object, not merged into the above
    name = obj.fields["Name"]
    assert name.fls.createable is True
    assert name.fls.updateable is True
    # Id is object-createable-context but field-level not createable: proves the
    # two axes are not collapsed (object createable=True, field createable=False)
    idf = obj.fields["Id"]
    assert idf.fls.createable is False
    assert obj.object_permissions.createable is True


def test_describe_missing_flag_is_unknown_not_false() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    rev = obj.fields["AnnualRevenue"]
    # updateable was omitted from the fixture -> UNKNOWN, never fabricated False
    assert rev.fls.updateable is UNKNOWN
    assert rev.fls.createable is True


def test_unknown_sentinel_is_not_truth_valued() -> None:
    with pytest.raises(TypeError):
        bool(UNKNOWN)


def test_describe_source_kind_is_search_snippet_corroborated() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    assert obj.source_kind is SourceKind.SEARCH_SNIPPET_CORROBORATED


def test_describe_rejects_object_without_name() -> None:
    with pytest.raises(ValueError):
        parse_object_describe({"fields": []})


def test_describe_rejects_nameless_field() -> None:
    with pytest.raises(ValueError):
        parse_object_describe({"name": "Account", "fields": [{"type": "string"}]})


def test_describe_rejects_non_object_field_entry() -> None:
    with pytest.raises(ValueError):
        parse_object_describe({"name": "Account", "fields": ["not-a-dict"]})


# --------------------------------------------------------------------------- #
# typed payload parsing
# --------------------------------------------------------------------------- #


def test_parse_record_types_fields() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    parsed = parse_record(
        obj,
        {
            "attributes": {"type": "Account", "url": "/x"},
            "Id": "001A",
            "Name": "Acme",
            "AnnualRevenue": 1000,  # int accepted for currency, preserved as int
            "IsDeleted": False,
        },
    )
    assert parsed["Id"] == "001A"
    # an integer numeric value is preserved as an int (no lossy float() coercion)
    assert parsed["AnnualRevenue"] == 1000
    assert isinstance(parsed["AnnualRevenue"], int)
    assert parsed["IsDeleted"] is False
    # attributes envelope preserved verbatim
    assert parsed["attributes"] == {"type": "Account", "url": "/x"}


def test_numeric_field_preserves_large_integer_precision() -> None:
    # An integer above float's exact ceiling (2**53) must NOT be corrupted by a
    # silent float() coercion. 9007199254740993 == 2**53 + 1 is unrepresentable
    # as a float and would collapse to 9007199254740992.0 if coerced.
    obj = parse_object_describe(fixtures.account_describe())
    big = 9007199254740993
    out = parse_typed_field(obj, "AnnualRevenue", big)
    assert out == big
    assert isinstance(out, int)
    assert out != float(big)  # the coercion the fix avoids would have lost this


def test_numeric_field_keeps_float_as_float() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    out = parse_typed_field(obj, "AnnualRevenue", 12.5)
    assert out == 12.5
    assert isinstance(out, float)


def test_parse_typed_field_none_is_preserved() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    assert parse_typed_field(obj, "AnnualRevenue", None) is None


def test_parse_typed_field_rejects_undescribed_field() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "NotAField", "x")


def test_parse_typed_field_rejects_wrong_type() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "Name", 123)  # string field, int value


def test_parse_typed_field_boolean_not_accepted_for_number() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "AnnualRevenue", True)  # bool must not pass as number


def test_parse_typed_field_number_not_accepted_for_boolean() -> None:
    obj = parse_object_describe(fixtures.account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "IsDeleted", 1)  # int must not pass as boolean


# --------------------------------------------------------------------------- #
# error classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,expected",
    [
        ("INVALID_SESSION_ID", SalesforceErrorCategory.AUTH),
        ("INSUFFICIENT_ACCESS", SalesforceErrorCategory.FORBIDDEN),
        ("REQUEST_LIMIT_EXCEEDED", SalesforceErrorCategory.QUOTA),
        ("DUPLICATE_VALUE", SalesforceErrorCategory.CONFLICT),
        ("MALFORMED_QUERY", SalesforceErrorCategory.INPUT),
    ],
)
def test_classify_vendor_error_code(code: str, expected: SalesforceErrorCategory) -> None:
    assert classify_vendor_error_code(code) is expected


def test_classify_vendor_error_code_unknown_is_none() -> None:
    assert classify_vendor_error_code("SOME_FUTURE_CODE") is None


def test_classify_timeout_is_ambiguous_not_temporary() -> None:
    # The critical negative: an ambiguous timeout must NOT be a retry-safe class.
    assert classify_status(None, timed_out=True) is SalesforceErrorCategory.AMBIGUOUS
    assert classify_status(504, timed_out=True) is SalesforceErrorCategory.AMBIGUOUS


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, SalesforceErrorCategory.AUTH),
        (403, SalesforceErrorCategory.FORBIDDEN),
        (404, SalesforceErrorCategory.NOT_FOUND),
        (409, SalesforceErrorCategory.CONFLICT),
        (429, SalesforceErrorCategory.THROTTLE),
        (400, SalesforceErrorCategory.INPUT),
        (503, SalesforceErrorCategory.TEMPORARY),
        (418, SalesforceErrorCategory.AMBIGUOUS),
    ],
)
def test_classify_status(status: int, expected: SalesforceErrorCategory) -> None:
    assert classify_status(status) is expected


def test_classify_none_status_is_ambiguous() -> None:
    assert classify_status(None) is SalesforceErrorCategory.AMBIGUOUS


# --------------------------------------------------------------------------- #
# query locator: REST pagination, two real pages
# --------------------------------------------------------------------------- #


def test_two_page_real_pagination_cursor_monotonic_no_dupes() -> None:
    """Drive two real pages: cursor advances, ids unique, terminal detectable."""
    page1 = parse_query_page(fixtures.query_page_first())
    assert page1.done is False
    assert page1.next_records_url  # non-terminal carries a locator
    assert page1.is_terminal is False

    seen_ids: list[str] = [r["Id"] for r in page1.records]
    # checkpoint only advances after the page is fully consumed
    assert next_locator(page1, page_fully_consumed=False) is None  # do-not-advance
    cursor = next_locator(page1, page_fully_consumed=True)
    assert cursor == page1.next_records_url  # cursor moved forward to page 2

    page2 = parse_query_page(fixtures.query_page_terminal())
    assert page2.done is True
    assert page2.is_terminal is True
    assert page2.next_records_url is None  # terminal detectable

    seen_ids.extend(r["Id"] for r in page2.records)
    # no duplicate ids across the two pages -> cursor did not re-serve a page
    assert len(seen_ids) == len(set(seen_ids))
    # consuming the terminal page yields no further cursor
    assert next_locator(page2, page_fully_consumed=True) is None


def test_query_page_done_false_requires_locator() -> None:
    with pytest.raises(QueryPaginationError):
        parse_query_page({"done": False, "totalSize": 1, "records": []})


def test_query_page_done_true_forbids_locator() -> None:
    with pytest.raises(QueryPaginationError):
        parse_query_page({"done": True, "totalSize": 1, "records": [], "nextRecordsUrl": "/x"})


def test_query_page_done_must_be_bool() -> None:
    with pytest.raises(QueryPaginationError):
        parse_query_page({"done": "false", "totalSize": 1, "records": []})


def test_query_page_records_must_be_list() -> None:
    with pytest.raises(QueryPaginationError):
        parse_query_page({"done": True, "totalSize": 1, "records": {}})


def test_query_page_ignores_soap_querymore_key() -> None:
    # A stray queryMore key must not be modeled (REST has no queryMore endpoint).
    page = parse_query_page({"done": True, "totalSize": 0, "records": [], "queryMore": "ignored"})
    assert page.is_terminal is True


# --------------------------------------------------------------------------- #
# bulk 2.0 partial-results contract
# --------------------------------------------------------------------------- #


def test_bulk_job_complete_is_downloadable_but_may_have_failures() -> None:
    state = parse_ingest_job_status(fixtures.bulk_job_complete())
    assert state == "JobComplete"
    assert results_downloadable(state) is True
    result = partition_row_outcomes(
        state,
        successful_results=[{"sf__Id": "001A", "sf__Created": "true"}],
        failed_results=[{"sf__Error": "REQUIRED_FIELD_MISSING:Name", "Name": ""}],
    )
    # per-row fidelity: successful AND failed both preserved, not aggregated
    assert len(result.successful) == 1
    assert len(result.failed) == 1
    assert result.has_failures is True  # JobComplete + failed rows coexist
    assert result.failed[0].kind == "failed"
    assert result.failed[0].fields["sf__Error"].startswith("REQUIRED_FIELD_MISSING")


def test_bulk_unrecognized_state_rejected() -> None:
    with pytest.raises(BulkStateError):
        parse_ingest_job_status({"state": "Frobnicating"})


def test_bulk_missing_state_rejected() -> None:
    with pytest.raises(BulkStateError):
        parse_ingest_job_status({})


def test_bulk_in_progress_not_downloadable() -> None:
    assert results_downloadable("InProgress") is False
    assert results_downloadable("Open") is False
    assert results_downloadable("UploadComplete") is False


def test_bulk_results_before_downloadable_rejected() -> None:
    # Supplying per-row results for an in-flight job is a caller bug -> surfaced.
    with pytest.raises(BulkStateError):
        partition_row_outcomes("InProgress", successful_results=[{"sf__Id": "x"}])


def test_bulk_failed_job_yields_unprocessed() -> None:
    result = partition_row_outcomes("Failed", unprocessed_records=[{"Name": "never processed"}])
    assert len(result.unprocessed) == 1
    assert result.unprocessed[0].kind == "unprocessed"
    assert result.has_failures is True


def test_bulk_states_closed_set() -> None:
    from kiro_crew.connections.vendors.salesforce import BULK_INGEST_STATES

    assert "JobComplete" in BULK_INGEST_STATES
    assert "queryMore" not in BULK_INGEST_STATES


# --------------------------------------------------------------------------- #
# idempotency closed set
# --------------------------------------------------------------------------- #


def test_idempotency_external_id_upsert() -> None:
    assert idempotency_class_for(has_external_id=True) is IdempotencyClass.EXTERNAL_ID_UPSERT


def test_idempotency_keyless_is_verify_by_readback_never_blind_retry() -> None:
    # The critical invariant: a keyless (timeout-ambiguous) write resolves to
    # verify-by-readback -- there is no third "just retry" class to return.
    cls = idempotency_class_for(has_external_id=False)
    assert cls is IdempotencyClass.NONE_VERIFY_BY_READBACK


def test_idempotency_class_is_closed_set() -> None:
    assert IDEMPOTENCY_CLASSES == {
        "base_sha_guard",
        "generate_ids_preallocation",
        "external_id_upsert",
        "none_verify_by_readback",
    }


def test_idempotency_salesforce_applicable_subset() -> None:
    assert is_salesforce_applicable("external_id_upsert") is True
    assert is_salesforce_applicable("none_verify_by_readback") is True
    # GitHub-shaped classes are in the closed enum but not claimed for Salesforce
    assert is_salesforce_applicable("base_sha_guard") is False
    assert is_salesforce_applicable("generate_ids_preallocation") is False
    # a value outside the closed set is unknown
    assert is_salesforce_applicable("made_up_class") is None


# --------------------------------------------------------------------------- #
# Apex capability gate
# --------------------------------------------------------------------------- #


def test_register_authorized_rest_resource() -> None:
    res = register_authorized_rest_resource(
        class_name="AccountService",
        method_name="getAccount",
        url_mapping="/AccountService/*",
        http_annotation="HttpGet",
        authorized_methods=["AccountService.getAccount"],
    )
    assert res.class_name == "AccountService"
    assert res.http_annotation == "HttpGet"


@pytest.mark.parametrize(
    "annotation",
    ["HttpGet", "HttpPost", "HttpPut", "HttpPatch", "HttpDelete"],
)
def test_register_accepts_every_allowed_http_annotation(annotation: str) -> None:
    # Round-trip every documented member of the allow-list so a future edit that
    # drops or mistypes one is caught, not silently narrowed.
    from kiro_crew.connections.vendors.salesforce.apex import _ALLOWED_HTTP_ANNOTATIONS

    assert annotation in _ALLOWED_HTTP_ANNOTATIONS
    res = register_authorized_rest_resource(
        class_name="Svc",
        method_name="op",
        url_mapping="/Svc/*",
        http_annotation=annotation,
        authorized_methods=["Svc.op"],
    )
    assert res.http_annotation == annotation


def test_allowed_http_annotation_set_is_exactly_the_five_apex_verbs() -> None:
    from kiro_crew.connections.vendors.salesforce.apex import _ALLOWED_HTTP_ANNOTATIONS

    assert _ALLOWED_HTTP_ANNOTATIONS == frozenset(
        {"HttpGet", "HttpPost", "HttpPut", "HttpPatch", "HttpDelete"}
    )


def test_register_refuses_unauthorized_method() -> None:
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="deleteEverything",
            url_mapping="/AccountService/*",
            http_annotation="HttpDelete",
            authorized_methods=["AccountService.getAccount"],  # not this method
        )


def test_register_refuses_bad_http_annotation() -> None:
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="getAccount",
            url_mapping="/AccountService/*",
            http_annotation="HttpTeapot",
            authorized_methods=["AccountService.getAccount"],
        )


def test_register_refuses_empty_url_mapping() -> None:
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="getAccount",
            url_mapping="",
            http_annotation="HttpGet",
            authorized_methods=["AccountService.getAccount"],
        )


def test_execute_anonymous_is_always_rejected() -> None:
    # The negative test the acceptance requires: the reject path actually fires.
    with pytest.raises(ApexCapabilityError):
        reject_execute_anonymous("System.debug('anything');")
    # and with no arguments at all
    with pytest.raises(ApexCapabilityError):
        reject_execute_anonymous()
