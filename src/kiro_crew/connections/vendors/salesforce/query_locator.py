"""REST query-locator pagination contract.

Salesforce's REST query endpoint (``GET .../query?q=...``) returns one page of a
result set as ``{totalSize, done, records, [nextRecordsUrl]}``. When ``done`` is
``false`` the response carries a ``nextRecordsUrl`` locator; the caller fetches
that URL for the next page and continues until ``done`` is ``true``. (Facts
search-snippet corroborated: ``developer.salesforce.com`` rejects automated
fetches with HTTP 403.)

This is the **REST** contract and only the REST contract. ``queryMore`` is the
SOAP API's mechanism; REST has no ``queryMore`` endpoint, so this module does
not model one. The page-size hint is the ``Sforce-Query-Options`` request header
(``batchSize=N``); its corroborated bounds are minimum 200, default/maximum
2000, so :data:`MIN_BATCH_SIZE` is 200.

Checkpoint discipline (the reason this is a contract, not just a parser): a
caller advances its durable cursor ONLY after a whole page has been fully
consumed. :func:`next_locator` returns the cursor to persist, and it is defined
so that a page whose consumption failed leaves the caller pointing at the same
page it must re-fetch -- the checkpoint never jumps past an unconsumed page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

#: Minimum ``Sforce-Query-Options`` batchSize (corroborated: min 200, max 2000).
MIN_BATCH_SIZE = 200
#: Default/maximum batchSize (corroborated).
MAX_BATCH_SIZE = 2000


class QueryPaginationError(ValueError):
    """A query page did not match the REST query-result contract."""


@dataclass(frozen=True)
class QueryLocatorPage:
    """One page of a REST query result.

    ``next_records_url`` is the opaque ``nextRecordsUrl`` locator when ``done`` is
    ``False`` (more pages remain), and ``None`` when ``done`` is ``True`` (this is
    the terminal page). The two are checked for consistency at parse time: a
    ``done=False`` page MUST carry a locator, and a ``done=True`` page MUST NOT --
    an inconsistent pair is vendor drift the caller must see, not paper over.
    """

    total_size: int
    done: bool
    records: Sequence[Mapping[str, Any]]
    next_records_url: Optional[str]

    @property
    def is_terminal(self) -> bool:
        """Whether this is the last page (``done`` is true)."""

        return self.done


def parse_query_page(raw: Mapping[str, Any]) -> QueryLocatorPage:
    """Parse a REST query response into a :class:`QueryLocatorPage`.

    Enforces the REST contract:

    * ``done`` must be a real boolean.
    * ``records`` must be a list.
    * ``done=False`` requires a non-empty string ``nextRecordsUrl``; ``done=True``
      forbids one. This makes a terminal page detectable structurally, per the
      acceptance requirement.

    A ``queryMore`` key, if ever present, is ignored -- it is not part of the REST
    contract and modeling it would imply a SOAP mechanism this core does not
    implement.
    """

    done = raw.get("done")
    if not isinstance(done, bool):
        raise QueryPaginationError("query page 'done' must be a boolean")
    records = raw.get("records")
    if not isinstance(records, list):
        raise QueryPaginationError("query page 'records' must be a list")
    locator = raw.get("nextRecordsUrl")
    if done:
        if locator is not None:
            raise QueryPaginationError(
                "a terminal page (done=true) must not carry a nextRecordsUrl"
            )
    else:
        if not isinstance(locator, str) or not locator:
            raise QueryPaginationError(
                "a non-terminal page (done=false) must carry a non-empty " "nextRecordsUrl locator"
            )
    total_size = raw.get("totalSize")
    if not isinstance(total_size, int) or isinstance(total_size, bool):
        raise QueryPaginationError("query page 'totalSize' must be an integer")
    return QueryLocatorPage(
        total_size=total_size,
        done=done,
        records=records,
        next_records_url=locator if not done else None,
    )


def next_locator(page: QueryLocatorPage, *, page_fully_consumed: bool) -> Optional[str]:
    """Return the cursor to persist AFTER attempting to consume ``page``.

    The checkpoint contract in one function:

    * If the page was NOT fully consumed, return ``None`` to signal "do not
      advance" -- the caller keeps the cursor it already holds and re-fetches the
      same page. The checkpoint never moves past an unconsumed page.
    * If the page WAS fully consumed and it is terminal (``done``), there is no
      further cursor: return ``None`` meaning "complete", which the caller
      distinguishes from the not-consumed case by the flag it passed in.
    * If the page WAS fully consumed and more pages remain, return the
      ``nextRecordsUrl`` locator -- the cursor the caller now persists and fetches
      next.

    The two ``None`` returns are not ambiguous to a correct caller: it knows
    whether it consumed the page, so it reads ``None`` as "retry this page" only
    in the not-consumed branch and as "done" only in the consumed-terminal
    branch. Callers that need an unambiguous signal should branch on
    ``page.is_terminal`` and ``page_fully_consumed`` directly; this helper
    encodes the advance rule, not a total state machine.
    """

    if not page_fully_consumed:
        return None
    if page.is_terminal:
        return None
    return page.next_records_url
