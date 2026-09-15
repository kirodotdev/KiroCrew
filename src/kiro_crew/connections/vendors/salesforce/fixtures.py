"""Offline test fixtures for the Salesforce core.

Every fixture here is an OFFLINE fixture: a hand-built dict shaped to the
Salesforce response structure the L1 core parses, NOT a capture of a live call.
Each carries an explicit ``source_kind`` of ``search_snippet_corroborated`` and a
``source`` note pointing at the corroborating evidence, so a reader can never
mistake one of these for real vendor output. No fixture claims
``official_docs``; ``developer.salesforce.com`` rejected every automated fetch
with HTTP 403.
"""

from __future__ import annotations

from typing import Any, Mapping

#: The uniform provenance stamp for every fixture in this module.
FIXTURE_SOURCE_KIND = "search_snippet_corroborated"
FIXTURE_SOURCE = (
    "hand-built offline fixture shaped from search-snippet-corroborated Salesforce "
    "response structure; developer.salesforce.com returns HTTP 403 to automated "
    "fetches, so no fixture is a fully-rendered-official-page capture, and none is "
    "a live-call capture"
)


def account_describe() -> Mapping[str, Any]:
    """A minimal Account sObject describe, FLS and object permissions distinct."""

    return {
        "source_kind": FIXTURE_SOURCE_KIND,
        "source": FIXTURE_SOURCE,
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


def query_page_first(
    next_url: str = "/services/data/v60.0/query/01g000000000001AAA-200",
) -> Mapping[str, Any]:
    """A non-terminal REST query page (done=false, carries a locator)."""

    return {
        "source_kind": FIXTURE_SOURCE_KIND,
        "source": FIXTURE_SOURCE,
        "totalSize": 350,
        "done": False,
        "nextRecordsUrl": next_url,
        "records": [
            {"attributes": {"type": "Account", "url": "/x/1"}, "Id": "001A", "Name": "Acme"},
            {"attributes": {"type": "Account", "url": "/x/2"}, "Id": "001B", "Name": "Globex"},
        ],
    }


def query_page_terminal() -> Mapping[str, Any]:
    """A terminal REST query page (done=true, no locator)."""

    return {
        "source_kind": FIXTURE_SOURCE_KIND,
        "source": FIXTURE_SOURCE,
        "totalSize": 350,
        "done": True,
        "records": [
            {"attributes": {"type": "Account", "url": "/x/3"}, "Id": "001C", "Name": "Initech"},
        ],
    }


def bulk_job_complete() -> Mapping[str, Any]:
    """A Bulk 2.0 ingest job-info payload in the JobComplete state."""

    return {
        "source_kind": FIXTURE_SOURCE_KIND,
        "source": FIXTURE_SOURCE,
        "id": "750xx000000000AAA",
        "state": "JobComplete",
        "object": "Account",
        "operation": "insert",
    }
