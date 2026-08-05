"""Local folder connector for knowledge sources."""

from pathlib import Path

from .base import BaseConnector


class LocalFolderConnector(BaseConnector):
    """Connector for local directory sources. Validates path; scanning handled by FolderWatcher."""

    async def fetch(self, source: dict) -> tuple[str, dict]:
        # Folder sources don't use fetch — FolderWatcher handles per-file ingestion
        return "", {}

    async def detect_changes(self, source: dict) -> bool:
        # Change detection handled by FolderWatcher via mtime/hash
        return False

    def validate_config(self, config: dict) -> tuple[bool, str]:
        uri = config.get("url") or config.get("uri", "")
        if not uri:
            return False, "Folder path is required"
        p = Path(uri)
        if not p.exists():
            return False, f"Path does not exist: {uri}"
        if not p.is_dir():
            return False, f"Path is not a directory: {uri}"
        return True, ""

    def source_type(self) -> str:
        return "local_folder"
