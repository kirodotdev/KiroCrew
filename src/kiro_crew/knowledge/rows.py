"""Per-row ingest contract for structured connectors.

A structured connector (GitHub issues/PRs/commits/check-runs, Salesforce
records, ...) does not fetch one blob of text: it fetches MANY rows, each with
its OWN stable identity, its own content, and -- critically -- its own per-user
ACL. The old ``BaseConnector.fetch(source) -> (text, meta)`` path collapses that
to a single text blob whose per-row refs live only in ``source.properties``,
which the ingest never sees, so different-permission rows would share one chunk
and one (or no) grant.

:class:`SourceRow` is the shared DTO that carries a row's identity + content +
grant all the way to the item/chunk, so :meth:`IngestionPipeline.ingest_rows`
can bind each row to its own item group AND write its own
``set_item_acl(resource_ref=...)`` in the same durable unit. Nothing here talks
to a vendor: a connector builds these from its fetched rows; the shared pipeline
consumes them. This is the contract the two vendor connectors (SF, GitHub) fill
without any change to the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from .acl import PUBLIC_SUBJECT, ProviderResourceRef


@dataclass(frozen=True)
class SourceRow:
    """One fetched row of a structured source, with its own identity + ACL.

    * ``key``          -- the row's STABLE identity within its source (e.g. a
                          GitHub ``owner/repo#number``, a Salesforce record id).
                          It keys the row's item group across syncs, so an update
                          replaces exactly that row's items and an incremental
                          sync never touches unchanged rows. Required, non-empty.
    * ``text``         -- the row's own extracted text (this row only -- never a
                          concatenation of several rows, which would merge
                          distinct permissions into one chunk).
    * ``resource_ref`` -- the :class:`acl.ProviderResourceRef` naming WHICH
                          provider object this row is, persisted on the row's
                          grant so the query-time gate can resolve the binding
                          and revalidate. Required for a managed row.
    * ``subjects``     -- the subject ids allowed to see this row (or
                          ``acl.PUBLIC_SUBJECT``); the row's per-user ACL.
    * ``tenant``       -- the tenant the grant belongs to (or ``acl.PUBLIC_TENANT``).
    * ``managed``      -- True for a cloud/structured row whose ACL is enforced +
                          revalidated at query time (the normal case here).
    * ``title``        -- optional display title; defaults to ``key``.
    * ``item_type``    -- optional item type label (default 'document').
    """

    key: str
    text: str
    resource_ref: ProviderResourceRef | None = None
    subjects: tuple[str, ...] = (PUBLIC_SUBJECT,)
    tenant: str = ""
    managed: bool = True
    title: str | None = None
    item_type: str = "document"

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("SourceRow.key must be a non-empty stable row identity")
        if self.managed and self.resource_ref is None:
            # A managed row with no resource_ref cannot be located to revalidate,
            # so the query-time gate would deny it forever. Fail loudly at
            # construction rather than silently ingesting an unqueryable row.
            raise ValueError(
                f"SourceRow(key={self.key!r}) is managed but has no resource_ref; "
                "a managed row must carry the ProviderResourceRef that locates it "
                "for query-time revalidation."
            )

    @property
    def display_title(self) -> str:
        return self.title or self.key


@dataclass(frozen=True)
class RowResult:
    """The outcome of ingesting one :class:`SourceRow`."""

    key: str
    item_ids: tuple[str, ...] = ()
    changed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class RowsIngestOutcome:
    """The result of one :meth:`ingest_rows` call, read by the sync scheduler to
    decide whether the source checkpoint may advance.

    ``fully_persisted`` is True only when EVERY row's data AND its ACL grant were
    committed with no error and no cancellation -- the sync scheduler advances
    the source checkpoint only then, so a partial/failed/cancelled round leaves
    the checkpoint where it was and the next sync re-attempts the un-persisted
    rows. ``deleted_keys`` are row keys removed because a FULL snapshot no longer
    contained them (never populated for an incremental round).
    """

    results: tuple[RowResult, ...] = ()
    deleted_keys: tuple[str, ...] = ()
    fully_persisted: bool = False

    @property
    def rows_changed(self) -> int:
        return sum(1 for r in self.results if r.changed)
