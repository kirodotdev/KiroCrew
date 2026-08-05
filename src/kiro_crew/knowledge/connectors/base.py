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
