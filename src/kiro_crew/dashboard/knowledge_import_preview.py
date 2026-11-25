"""Read-only discovery for the owner's bounded Knowledge import card."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from kiro_crew import change_card_knowledge as imports
from kiro_crew.change_card_catalog_kinds import CardCatalogError
from kiro_crew.dashboard.handlers.knowledge_sources import _probe_local_uri
from kiro_crew.knowledge.folder_watcher import estimate_scan_cost, folder_chunk_budget, walk_filters


def read_sources(params: dict[str, Any], store: Any, watcher: Any) -> dict[str, Any]:
    """Run on the bounded filesystem probe pool; no content extraction or writes."""
    if store is None or watcher is None:
        raise CardCatalogError(
            "knowledge_unavailable", "the Knowledge folder watcher is unavailable"
        )
    entries: list[dict[str, Any]] = []
    roots: list[Path] = []
    for source in params["sources"]:
        path = Path(source["root"])
        if not path.is_absolute() or path.is_symlink() or path == Path.home():
            raise CardCatalogError(
                "invalid_root", "use a prepared local directory, not a link or home"
            )
        probe = _probe_local_uri(str(path))
        root = Path(probe.resolved)
        if probe.sensitive or not probe.is_dir or root == Path.home().resolve():
            raise CardCatalogError("invalid_root", "the prepared root is missing or restricted")
        if any(root.is_relative_to(p) or p.is_relative_to(root) for p in roots):
            raise CardCatalogError("overlapping_roots", "prepared roots must not overlap")
        roots.append(root)
        props = imports.properties(source)
        discovered = watcher._walk(str(root), **walk_filters(props, "local_folder"))
        if not discovered or len(discovered) > imports.MAX_FILES:
            raise CardCatalogError(
                "source_file_limit", "each source needs 1 to 1000 eligible files"
            )
        cost = estimate_scan_cost(discovered, max_files=imports.MAX_FILES)
        row = store.get_source_by_uri(str(root))
        existing = None
        if row:
            if row["source_type"] != "local_folder":
                raise CardCatalogError(
                    "source_conflict", "this URI belongs to a different source type"
                )
            try:
                stored = json.loads(row["properties"] or "{}")
            except (TypeError, ValueError):
                raise CardCatalogError(
                    "source_conflict", "the existing source has unreadable properties"
                ) from None
            if not isinstance(stored, dict):
                raise CardCatalogError(
                    "source_conflict", "the existing source has invalid properties"
                )
            existing = {
                "id": row["id"],
                "source_type": row["source_type"],
                "sync_status": row["sync_status"],
                "properties": {k: stored.get(k) for k in props},
            }
        entries.append(
            {
                "uri": str(root),
                "files": len(discovered),
                "estimated_chunks": cost["chunks"],
                "estimated_llm_calls": cost["llm_calls"],
                "chunk_budget_per_sweep": folder_chunk_budget(props) or 0,
                "files_revision": hashlib.sha256(
                    json.dumps(sorted(discovered)).encode()
                ).hexdigest(),
                "existing": existing,
            }
        )
    return {"sources": entries}
