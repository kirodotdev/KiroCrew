"""Salesforce vendor-offline core (W10 / L1).

A pure, offline model of the Salesforce data surface: the describe-driven
object/field model, typed payload parsing, an error-classification skeleton,
the REST query-locator pagination contract, the Bulk API 2.0 partial-results
contract, and the Apex ``@RestResource`` capability gate. **No runtime wiring**
-- nothing here mints a token, opens a socket, or reads governance state. The
shared control plane (W10's own binding/auth/policy/retry, layered on W01)
consumes this core at dispatch time.

Evidence discipline: every Salesforce fact encoded here is
``search_snippet_corroborated`` -- ``developer.salesforce.com`` fully rejects
automated fetches (HTTP 403), so no field below is backed by a fully rendered
official page. Facts that could not be corroborated at all are kept as the
explicit ``UNKNOWN`` sentinel, named rather than invented. See
``docs/system-specs/modules/connector-salesforce-core.md`` for the field-by-field
provenance table.
"""

from kiro_crew.connections.vendors.salesforce.apex import (
    ApexCapabilityError,
    ApexRestResource,
    register_authorized_rest_resource,
    reject_execute_anonymous,
)
from kiro_crew.connections.vendors.salesforce.bulk import (
    BULK_INGEST_STATES,
    BulkIngestResult,
    BulkRowOutcome,
    BulkStateError,
    parse_ingest_job_status,
    partition_row_outcomes,
    results_downloadable,
)
from kiro_crew.connections.vendors.salesforce.describe import (
    UNKNOWN,
    FieldDescribe,
    FieldLevelSecurity,
    ObjectDescribe,
    ObjectPermissions,
    SourceKind,
    Ternary,
    parse_field_describe,
    parse_object_describe,
)
from kiro_crew.connections.vendors.salesforce.errors import (
    SalesforceErrorCategory,
    classify_status,
    classify_vendor_error_code,
)
from kiro_crew.connections.vendors.salesforce.idempotency import (
    IDEMPOTENCY_CLASSES,
    IdempotencyClass,
    idempotency_class_for,
)
from kiro_crew.connections.vendors.salesforce.payload import (
    PayloadParseError,
    parse_record,
    parse_typed_field,
)
from kiro_crew.connections.vendors.salesforce.query_locator import (
    QueryLocatorPage,
    QueryPaginationError,
    next_locator,
    parse_query_page,
)

__all__ = [
    "UNKNOWN",
    "BULK_INGEST_STATES",
    "IDEMPOTENCY_CLASSES",
    "ApexCapabilityError",
    "ApexRestResource",
    "BulkIngestResult",
    "BulkRowOutcome",
    "BulkStateError",
    "FieldDescribe",
    "FieldLevelSecurity",
    "IdempotencyClass",
    "ObjectDescribe",
    "ObjectPermissions",
    "PayloadParseError",
    "QueryLocatorPage",
    "QueryPaginationError",
    "SalesforceErrorCategory",
    "SourceKind",
    "Ternary",
    "classify_status",
    "classify_vendor_error_code",
    "idempotency_class_for",
    "next_locator",
    "parse_field_describe",
    "parse_ingest_job_status",
    "parse_object_describe",
    "parse_query_page",
    "parse_record",
    "parse_typed_field",
    "partition_row_outcomes",
    "register_authorized_rest_resource",
    "reject_execute_anonymous",
    "results_downloadable",
]
