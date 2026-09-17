"""Tests for the Salesforce vendor-offline core (W10 / L1).

Covers the positive contract and, emphatically, the negative paths: malformed
describe, mistyped payloads, pagination-contract violations, unrecognized bulk
states, results-before-downloadable, anonymous-Apex rejection, and the FLS /
object-permission separation. All fixtures are offline and stamped as such.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from kiro_crew.connections.vendors.salesforce import (
    UNKNOWN,
    ApexCapabilityError,
    BulkStateError,
    IdempotencyClass,
    PayloadParseError,
    QueryPaginationError,
    classify_status,
    classify_vendor_error_code,
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
    _SALESFORCE_APPLICABLE,
    IDEMPOTENCY_CLASSES,
)

# --------------------------------------------------------------------------- #
# offline fixtures (inlined; only the tests consume them, so they do not ship
# in the wheel as a package module)
# --------------------------------------------------------------------------- #

#: The uniform provenance stamp for every fixture below.
_FIXTURE_SOURCE_KIND = "search_snippet_corroborated"
_FIXTURE_SOURCE = (
    "hand-built offline fixture shaped from search-snippet-corroborated Salesforce "
    "response structure; developer.salesforce.com returns HTTP 403 to automated "
    "fetches, so no fixture is a fully-rendered-official-page capture, and none is "
    "a live-call capture"
)


def _account_describe() -> Mapping[str, Any]:
    """A minimal Account sObject describe, FLS and object permissions distinct."""

    return {
        "source_kind": _FIXTURE_SOURCE_KIND,
        "source": _FIXTURE_SOURCE,
        "name": "Account",
        "label": "Account",
        # object-level permissions (distinct axis from per-field FLS)
        "createable": True,
        "queryable": True,
        "updateable": True,
        "deletable": False,
        "fields": [
            {
                "name": "Id",
                "type": "id",
                "soapType": "tns:ID",
                "nillable": False,
                "createable": False,
                "updateable": False,
                "accessible": True,
            },
            {
                "name": "Name",
                "type": "string",
                "soapType": "xsd:string",
                "nillable": False,
                "createable": True,
                "updateable": True,
                "accessible": True,
            },
            {
                "name": "AnnualRevenue",
                "type": "currency",
                "soapType": "xsd:double",
                "nillable": True,
                "createable": True,
                # updateable deliberately OMITTED -> parses to UNKNOWN, not False
                "accessible": True,
            },
            {
                "name": "IsDeleted",
                "type": "boolean",
                "soapType": "xsd:boolean",
                "nillable": False,
                "createable": False,
                "updateable": False,
                "accessible": True,
            },
        ],
    }


def _query_page_first(
    next_url: str = "/services/data/v60.0/query/01g000000000001AAA-200",
) -> Mapping[str, Any]:
    """A non-terminal REST query page (done=false, carries a locator)."""

    return {
        "source_kind": _FIXTURE_SOURCE_KIND,
        "source": _FIXTURE_SOURCE,
        "totalSize": 350,
        "done": False,
        "nextRecordsUrl": next_url,
        "records": [
            {"attributes": {"type": "Account", "url": "/x/1"}, "Id": "001A", "Name": "Acme"},
            {"attributes": {"type": "Account", "url": "/x/2"}, "Id": "001B", "Name": "Globex"},
        ],
    }


def _query_page_terminal() -> Mapping[str, Any]:
    """A terminal REST query page (done=true, no locator)."""

    return {
        "source_kind": _FIXTURE_SOURCE_KIND,
        "source": _FIXTURE_SOURCE,
        "totalSize": 350,
        "done": True,
        "records": [
            {"attributes": {"type": "Account", "url": "/x/3"}, "Id": "001C", "Name": "Initech"},
        ],
    }


def _bulk_job_complete() -> Mapping[str, Any]:
    """A Bulk 2.0 ingest job-info payload in the JobComplete state."""

    return {
        "source_kind": _FIXTURE_SOURCE_KIND,
        "source": _FIXTURE_SOURCE,
        "id": "750xx000000000AAA",
        "state": "JobComplete",
        "object": "Account",
        "operation": "insert",
    }


# --------------------------------------------------------------------------- #
# describe: FLS and object permissions are separate axes
# --------------------------------------------------------------------------- #


def test_describe_keeps_fls_and_object_permissions_distinct() -> None:
    obj = parse_object_describe(_account_describe())
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
    obj = parse_object_describe(_account_describe())
    rev = obj.fields["AnnualRevenue"]
    # updateable was omitted from the fixture -> UNKNOWN, never fabricated False
    assert rev.fls.updateable is UNKNOWN
    assert rev.fls.createable is True


def test_unknown_sentinel_is_not_truth_valued() -> None:
    with pytest.raises(TypeError):
        bool(UNKNOWN)


def test_describe_source_kind_is_search_snippet_corroborated() -> None:
    obj = parse_object_describe(_account_describe())
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
    obj = parse_object_describe(_account_describe())
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
    obj = parse_object_describe(_account_describe())
    big = 9007199254740993
    out = parse_typed_field(obj, "AnnualRevenue", big)
    assert out == big
    assert isinstance(out, int)
    assert out != float(big)  # the coercion the fix avoids would have lost this


def test_numeric_field_keeps_float_as_float() -> None:
    obj = parse_object_describe(_account_describe())
    out = parse_typed_field(obj, "AnnualRevenue", 12.5)
    assert out == 12.5
    assert isinstance(out, float)


def test_parse_typed_field_none_is_preserved() -> None:
    obj = parse_object_describe(_account_describe())
    assert parse_typed_field(obj, "AnnualRevenue", None) is None


def test_parse_typed_field_rejects_undescribed_field() -> None:
    obj = parse_object_describe(_account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "NotAField", "x")


def test_parse_typed_field_rejects_wrong_type() -> None:
    obj = parse_object_describe(_account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "Name", 123)  # string field, int value


def test_parse_typed_field_boolean_not_accepted_for_number() -> None:
    obj = parse_object_describe(_account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "AnnualRevenue", True)  # bool must not pass as number


def test_parse_typed_field_number_not_accepted_for_boolean() -> None:
    obj = parse_object_describe(_account_describe())
    with pytest.raises(PayloadParseError):
        parse_typed_field(obj, "IsDeleted", 1)  # int must not pass as boolean


# --------------------------------------------------------------------------- #
# typed payload parsing: relationship traversal + aggregate aliases (A)
# --------------------------------------------------------------------------- #


def _user_describe() -> Mapping[str, Any]:
    """A minimal User sObject describe, for the related-object parse path."""

    return {
        "name": "User",
        "label": "User",
        "queryable": True,
        "fields": [
            {"name": "Name", "type": "string", "nillable": True, "accessible": True},
        ],
    }


def test_parse_record_relationship_subobject_no_describe_passthrough() -> None:
    # SELECT Owner.Name FROM Account nests a child record under "Owner". With no
    # describe supplied for User, the child is preserved VERBATIM under UNKNOWN
    # semantics -- its attributes envelope kept, its own values untouched, no
    # guessed type, no coercion, and (crucially) NOT a hard failure.
    obj = parse_object_describe(_account_describe())
    parsed = parse_record(
        obj,
        {
            "attributes": {"type": "Account", "url": "/x"},
            "Id": "001A",
            "Name": "Acme",
            "Owner": {
                "attributes": {"type": "User", "url": "/u/1"},
                "Name": "Alice",
            },
        },
    )
    assert parsed["Id"] == "001A"
    assert parsed["Owner"]["attributes"] == {"type": "User", "url": "/u/1"}
    assert parsed["Owner"]["Name"] == "Alice"  # preserved verbatim, not coerced


def test_parse_record_relationship_subobject_with_related_describe() -> None:
    # When a describe for the related object IS supplied, the child is parsed
    # against it (so a mistyped related field is caught too).
    obj = parse_object_describe(_account_describe())
    user = parse_object_describe(_user_describe())
    parsed = parse_record(
        obj,
        {
            "attributes": {"type": "Account", "url": "/x"},
            "Name": "Acme",
            "Owner": {"attributes": {"type": "User", "url": "/u/1"}, "Name": "Alice"},
        },
        related={"User": user},
    )
    assert parsed["Owner"]["Name"] == "Alice"


def test_parse_record_relationship_subobject_related_describe_typechecks() -> None:
    # A related-object field that violates its own describe type still raises --
    # recursion does not weaken type checking where a describe exists.
    obj = parse_object_describe(_account_describe())
    user = parse_object_describe(_user_describe())
    with pytest.raises(PayloadParseError):
        parse_record(
            obj,
            {
                "attributes": {"type": "Account", "url": "/x"},
                "Name": "Acme",
                "Owner": {"attributes": {"type": "User", "url": "/u/1"}, "Name": 123},
            },
            related={"User": user},
        )


def test_parse_record_aggregate_alias_passthrough() -> None:
    # SELECT COUNT(Id) FROM Account -> {"expr0": 12}. An exprN alias is a computed
    # column with no field of its own; it is preserved verbatim, NOT rejected as
    # an undescribed field and NOT parsed as a typed field.
    obj = parse_object_describe(_account_describe())
    parsed = parse_record(
        obj,
        {"attributes": {"type": "AggregateResult"}, "expr0": 12, "expr1": "2026-01-01"},
    )
    assert parsed["expr0"] == 12
    assert parsed["expr1"] == "2026-01-01"


def test_parse_record_undescribed_scalar_field_still_raises() -> None:
    # The negative that proves relationship/aggregate handling did NOT broaden
    # the rule: a plain scalar key that is neither an exprN alias nor a nested
    # relationship record, and is not in the describe, is a field-name typo and
    # still hard-fails -- typo detection is intact.
    obj = parse_object_describe(_account_describe())
    with pytest.raises(PayloadParseError):
        parse_record(
            obj,
            {"attributes": {"type": "Account", "url": "/x"}, "Naem": "typo"},
        )


def test_parse_record_expr_like_but_not_alias_still_raises() -> None:
    # A key that merely resembles an alias ("expr" with no digits, or "expression")
    # is NOT an aggregate alias and is treated as an ordinary undescribed field,
    # so it still raises -- the alias match is anchored to exactly exprN.
    obj = parse_object_describe(_account_describe())
    with pytest.raises(PayloadParseError):
        parse_record(
            obj,
            {"attributes": {"type": "Account", "url": "/x"}, "expression": 1},
        )


# --------------------------------------------------------------------------- #
# typed payload: nullable relationships + parent-to-child subqueries (A, F1)
# --------------------------------------------------------------------------- #


def _account_describe_with_relationships() -> Mapping[str, Any]:
    """Account describe carrying a reference field's relationshipName + a
    childRelationships entry, so the parser knows the declared relationship
    names."""

    return {
        "name": "Account",
        "queryable": True,
        "fields": [
            {"name": "Id", "type": "id", "accessible": True},
            {"name": "Name", "type": "string", "accessible": True},
            # a reference field: OwnerId nests under relationshipName "Owner"
            {
                "name": "OwnerId",
                "type": "reference",
                "relationshipName": "Owner",
                "nillable": True,
                "accessible": True,
            },
            # a nullable custom parent lookup: Parent__c -> Parent__r
            {
                "name": "Parent__c",
                "type": "reference",
                "relationshipName": "Parent__r",
                "nillable": True,
                "accessible": True,
            },
        ],
        "childRelationships": [
            {"relationshipName": "Contacts", "childSObject": "Contact"},
            # a childRelationships entry with no relationshipName must be ignored
            {"childSObject": "Case"},
        ],
    }


def test_nullable_parent_relationship_null_is_accepted() -> None:
    # F1: a valid record with an EMPTY optional parent lookup (Parent__r: null)
    # must NOT crash the whole parse. Before the fix this fell through to
    # parse_typed_field, whose undescribed-key raise fired before the None
    # short-circuit, blowing up an entirely legitimate record.
    obj = parse_object_describe(_account_describe_with_relationships())
    parsed = parse_record(
        obj,
        {
            "attributes": {"type": "Account", "url": "/x"},
            "Id": "001A",
            "Name": "Acme",
            "Parent__r": None,  # empty optional parent lookup
            "Owner": None,  # empty owner reference
        },
    )
    assert parsed["Id"] == "001A"
    assert parsed["Parent__r"] is None
    assert parsed["Owner"] is None


def test_describe_exposes_declared_relationship_names() -> None:
    obj = parse_object_describe(_account_describe_with_relationships())
    names = obj.relationship_names()
    assert names == frozenset({"Owner", "Parent__r", "Contacts"})
    # the nameless childRelationships entry was ignored, not invented
    assert obj.child_relationships == ("Contacts",)


def test_populated_parent_relationship_still_parses() -> None:
    # A non-null parent under a declared relationship name is still a related
    # record and is parsed (verbatim without a related describe).
    obj = parse_object_describe(_account_describe_with_relationships())
    parsed = parse_record(
        obj,
        {
            "attributes": {"type": "Account", "url": "/x"},
            "Owner": {"attributes": {"type": "User", "url": "/u/1"}, "Name": "Alice"},
        },
    )
    assert parsed["Owner"]["Name"] == "Alice"


def test_parent_to_child_subquery_envelope_parsed_recordwise() -> None:
    # SELECT Id, (SELECT Id FROM Contacts) FROM Account nests a child-query
    # envelope; its records are parsed one by one and totalSize/done pass through.
    obj = parse_object_describe(_account_describe_with_relationships())
    parsed = parse_record(
        obj,
        {
            "attributes": {"type": "Account", "url": "/x"},
            "Id": "001A",
            "Contacts": {
                "totalSize": 2,
                "done": True,
                "records": [
                    {"attributes": {"type": "Contact", "url": "/c/1"}, "LastName": "A"},
                    {"attributes": {"type": "Contact", "url": "/c/2"}, "LastName": "B"},
                ],
            },
        },
    )
    assert parsed["Contacts"]["totalSize"] == 2  # envelope metadata verbatim
    assert parsed["Contacts"]["done"] is True
    assert [r["LastName"] for r in parsed["Contacts"]["records"]] == ["A", "B"]


def test_child_subquery_records_parsed_against_related_describe() -> None:
    # When the child object's describe is supplied, its records are type-checked.
    obj = parse_object_describe(_account_describe_with_relationships())
    contact = parse_object_describe(
        {
            "name": "Contact",
            "queryable": True,
            "fields": [{"name": "LastName", "type": "string", "accessible": True}],
        }
    )
    with pytest.raises(PayloadParseError):
        parse_record(
            obj,
            {
                "attributes": {"type": "Account", "url": "/x"},
                "Contacts": {
                    "totalSize": 1,
                    "done": True,
                    "records": [
                        {"attributes": {"type": "Contact"}, "LastName": 123},  # wrong type
                    ],
                },
            },
            related={"Contact": contact},
        )


def test_null_under_undeclared_key_still_raises() -> None:
    # The negative that keeps typo detection intact: a null under a key that is
    # NEITHER a declared relationship name NOR a describe field is a field-name
    # typo (e.g. a misspelled lookup) and still raises -- the null-acceptance is
    # scoped to declared relationship names, not to every null.
    obj = parse_object_describe(_account_describe_with_relationships())
    with pytest.raises(PayloadParseError):
        parse_record(
            obj,
            {"attributes": {"type": "Account", "url": "/x"}, "Parent__X": None},
        )


# --------------------------------------------------------------------------- #
# error classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,expected",
    [
        ("INVALID_SESSION_ID", "auth"),
        ("INSUFFICIENT_ACCESS", "forbidden"),
        ("REQUEST_LIMIT_EXCEEDED", "quota"),
        ("DUPLICATE_VALUE", "conflict"),
        ("MALFORMED_QUERY", "input"),
    ],
)
def test_classify_vendor_error_code(code: str, expected: str) -> None:
    assert classify_vendor_error_code(code) == expected


def test_classify_vendor_error_code_unknown_is_none() -> None:
    assert classify_vendor_error_code("SOME_FUTURE_CODE") is None


def test_classify_timeout_is_ambiguous_not_temporary() -> None:
    # The critical negative: an ambiguous timeout must NOT be a retry-safe class.
    assert classify_status(None, timed_out=True) == "ambiguous"
    assert classify_status(504, timed_out=True) == "ambiguous"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "auth"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (429, "throttle"),
        (400, "input"),
        (503, "temporary"),
        (418, "ambiguous"),
    ],
)
def test_classify_status(status: int, expected: str) -> None:
    assert classify_status(status) == expected


def test_classify_none_status_is_ambiguous() -> None:
    assert classify_status(None) == "ambiguous"


def test_classify_returns_shared_control_plane_class() -> None:
    # Every classified value is a member of the control plane's closed set --
    # this module maps INTO the shared taxonomy, it does not fork its own.
    from kiro_crew.connections.control_plane import ERROR_CLASSES

    assert classify_status(401) in ERROR_CLASSES
    assert classify_vendor_error_code("INVALID_SESSION_ID") in ERROR_CLASSES


# --------------------------------------------------------------------------- #
# query locator: REST pagination, two real pages
# --------------------------------------------------------------------------- #


def test_two_page_real_pagination_cursor_monotonic_no_dupes() -> None:
    """Drive two real pages: cursor advances, ids unique, terminal detectable."""
    page1 = parse_query_page(_query_page_first())
    assert page1.done is False
    assert page1.next_records_url  # non-terminal carries a locator
    assert page1.is_terminal is False

    seen_ids: list[str] = [r["Id"] for r in page1.records]
    # checkpoint only advances after the page is fully consumed
    assert next_locator(page1, page_fully_consumed=False) is None  # do-not-advance
    cursor = next_locator(page1, page_fully_consumed=True)
    assert cursor == page1.next_records_url  # cursor moved forward to page 2

    page2 = parse_query_page(_query_page_terminal())
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
    state = parse_ingest_job_status(_bulk_job_complete())
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

    # Exact set round-trip: if a documented member is dropped or an undocumented
    # one added, this fails -- a bare membership check would not catch a removal.
    assert BULK_INGEST_STATES == frozenset(
        {"Open", "UploadComplete", "InProgress", "JobComplete", "Failed", "Aborted"}
    )
    assert "JobComplete" in BULK_INGEST_STATES
    assert "queryMore" not in BULK_INGEST_STATES


def test_bulk_downloadable_states_exact_set() -> None:
    # The downloadable subset is exactly JobComplete/Failed/Aborted; the earlier
    # lifecycle states are not. An exact round-trip catches a member silently
    # disappearing from _DOWNLOADABLE_STATES, which a per-state spot check would
    # miss.
    from kiro_crew.connections.vendors.salesforce import results_downloadable
    from kiro_crew.connections.vendors.salesforce.bulk import (
        _DOWNLOADABLE_STATES,
        BULK_INGEST_STATES,
    )

    assert _DOWNLOADABLE_STATES == frozenset({"JobComplete", "Failed", "Aborted"})
    # every ingest state's downloadability matches membership in the subset
    for state in BULK_INGEST_STATES:
        assert results_downloadable(state) is (state in _DOWNLOADABLE_STATES)


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
    # The two classes that apply to Salesforce writes ARE in the applicable set...
    assert "external_id_upsert" in _SALESFORCE_APPLICABLE
    assert "none_verify_by_readback" in _SALESFORCE_APPLICABLE
    # ...and the GitHub-shaped classes, though in the closed manifest enum, are
    # NOT claimed for Salesforce.
    assert "base_sha_guard" not in _SALESFORCE_APPLICABLE
    assert "generate_ids_preallocation" not in _SALESFORCE_APPLICABLE
    # the applicable set is a strict subset of the closed manifest set
    assert _SALESFORCE_APPLICABLE < IDEMPOTENCY_CLASSES


# --------------------------------------------------------------------------- #
# Apex capability gate
# --------------------------------------------------------------------------- #


def test_register_authorized_rest_resource() -> None:
    res = register_authorized_rest_resource(
        class_name="AccountService",
        method_name="getAccount",
        url_mapping="/AccountService/*",
        http_annotation="HttpGet",
        authorized_methods=[
            ("AccountService", "getAccount", "/AccountService/*", "HttpGet"),
        ],
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
        authorized_methods=[("Svc", "op", "/Svc/*", annotation)],
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
            authorized_methods=[
                ("AccountService", "getAccount", "/AccountService/*", "HttpGet"),
            ],  # not this method
        )


def test_register_refuses_authorized_method_with_tampered_verb() -> None:
    # F1: an authorized class.method re-pointed at a DIFFERENT verb is a
    # different capability descriptor and must NOT register as authorized.
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="getAccount",
            url_mapping="/AccountService/*",
            http_annotation="HttpDelete",  # approved as HttpGet, not HttpDelete
            authorized_methods=[
                ("AccountService", "getAccount", "/AccountService/*", "HttpGet"),
            ],
        )


def test_register_refuses_authorized_method_with_tampered_path() -> None:
    # F1: an authorized class.method re-pointed at a DIFFERENT urlMapping is a
    # different capability descriptor and must NOT register as authorized.
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="getAccount",
            url_mapping="/AdminService/*",  # approved for /AccountService/*
            http_annotation="HttpGet",
            authorized_methods=[
                ("AccountService", "getAccount", "/AccountService/*", "HttpGet"),
            ],
        )


def test_register_accepts_apexrestresource_and_mapping_allowlist_forms() -> None:
    # The allow-list may carry a full ApexRestResource or a 4-field mapping;
    # both normalize to the same 4-tuple identity as a tuple entry.
    from kiro_crew.connections.vendors.salesforce.apex import ApexRestResource

    approved = ApexRestResource(
        class_name="Svc",
        method_name="op",
        url_mapping="/Svc/*",
        http_annotation="HttpPost",
    )
    res_obj = register_authorized_rest_resource(
        class_name="Svc",
        method_name="op",
        url_mapping="/Svc/*",
        http_annotation="HttpPost",
        authorized_methods=[approved],
    )
    assert res_obj == approved
    res_map = register_authorized_rest_resource(
        class_name="Svc",
        method_name="op",
        url_mapping="/Svc/*",
        http_annotation="HttpPost",
        authorized_methods=[
            {
                "class_name": "Svc",
                "method_name": "op",
                "url_mapping": "/Svc/*",
                "http_annotation": "HttpPost",
            }
        ],
    )
    assert res_map == approved


def test_register_refuses_partial_allowlist_entry() -> None:
    # A truncated allow-list entry (only class+method) must NOT be accepted as a
    # descriptor -- it would silently widen authorization back to identifier-only.
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="Svc",
            method_name="op",
            url_mapping="/Svc/*",
            http_annotation="HttpGet",
            authorized_methods=[("Svc", "op")],  # partial 2-tuple
        )


def test_register_refuses_bad_http_annotation() -> None:
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="getAccount",
            url_mapping="/AccountService/*",
            http_annotation="HttpTeapot",
            authorized_methods=[
                ("AccountService", "getAccount", "/AccountService/*", "HttpTeapot"),
            ],
        )


def test_register_refuses_empty_url_mapping() -> None:
    with pytest.raises(ApexCapabilityError):
        register_authorized_rest_resource(
            class_name="AccountService",
            method_name="getAccount",
            url_mapping="",
            http_annotation="HttpGet",
            authorized_methods=[
                ("AccountService", "getAccount", "/AccountService/*", "HttpGet"),
            ],
        )


def test_execute_anonymous_is_always_rejected() -> None:
    # The negative test the acceptance requires: the reject path actually fires.
    with pytest.raises(ApexCapabilityError):
        reject_execute_anonymous("System.debug('anything');")
    # and with no arguments at all
    with pytest.raises(ApexCapabilityError):
        reject_execute_anonymous()
