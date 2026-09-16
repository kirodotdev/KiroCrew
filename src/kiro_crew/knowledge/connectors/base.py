from abc import ABC, abstractmethod


class BaseConnector(ABC):
    """Base class for remote source connectors."""

    @abstractmethod
    async def fetch(self, source: dict) -> tuple[str, dict]:
        """Fetch content from source. Returns (text_content, metadata)."""
        ...

    @abstractmethod
    async def detect_changes(self, source: dict) -> bool:
        """Return True if source has changed since last sync."""
        ...

    @abstractmethod
    def validate_config(self, config: dict) -> tuple[bool, str]:
        """Validate source config. Returns (is_valid, error_message)."""
        ...

    @abstractmethod
    def source_type(self) -> str:
        """Return the source_type string (e.g., 'quip', 'sharepoint', 'url')."""
        ...

    def supports_rows(self) -> bool:
        """True if this connector fetches STRUCTURED ROWS (fetch_rows), not one
        blob of text (fetch). A structured connector overrides this + fetch_rows
        so the sync scheduler drives the per-row ACL ingest path; a plain-document
        connector leaves this False and keeps using fetch()/ingest_text."""
        return False

    async def fetch_rows(self, source: dict):
        """Fetch the source's rows for the per-row ingest contract.

        Returns ``(rows, snapshot, checkpoint)`` where ``rows`` is a list of
        ``kiro_crew.knowledge.rows.SourceRow`` (each carrying its own key, text,
        ProviderResourceRef and ACL grant), ``snapshot`` is True for a full fetch
        (absent rows may be deleted) / False for an incremental fetch (absent
        rows are untouched), and ``checkpoint`` is an opaque resume token the
        connector may persist. Only called when :meth:`supports_rows` is True."""
        raise NotImplementedError
