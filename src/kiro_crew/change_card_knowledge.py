"""A bounded folder-import proposal; only the owner's native source routes write."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable
from urllib.parse import quote

KIND = "knowledge.import_sources"
MAX_SOURCES = 4
MAX_FILES = 1000
SOURCE_FIELDS = {"root", "name", "namespace", "mode"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sources"],
    "properties": {
        "sources": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_SOURCES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["root", "name", "namespace", "mode"],
                "properties": {
                    "root": {"type": "string", "maxLength": 2048},
                    "name": {"type": "string", "maxLength": 200},
                    "namespace": {"type": "string", "maxLength": 64},
                    "mode": {"type": "string", "enum": ["documents", "code"]},
                },
            },
        },
    },
}

# Curated inputs still inherit the reader's sensitive-path and symlink checks.
IGNORE_PATTERNS = [
    "*[aA][gG][eE][nN][tT][sS].[mM][dD]",
    "*[sS][kK][iI][lL][lL].[mM][dD]",
    "*[cC][lL][aA][uU][dD][eE].[mM][dD]",
    "*[gG][eE][mM][iI][nN][iI].[mM][dD]",
    "*.env",
    "*.pem",
    "*.key",
    "*.db",
    "*.sqlite*",
    "*_gz.h",
    "*web_assets.h",
    "*web_page_gz.h",
    "*_generated.*",
    "fixtures/*",
    "*/fixtures/*",
    "stubs/*",
    "*/stubs/*",
    "vendor/*",
    "*/vendor/*",
    "_vendor/*",
    "*/_vendor/*",
]


def validate(params: dict[str, Any], *, fail: Callable[[str, str], Exception]) -> dict[str, Any]:
    """Accept only explicitly named roots; no arbitrary source properties or URLs."""
    if set(params) != {"sources"}:
        raise fail("invalid_param", "give only 'sources'")
    sources = params["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise fail("invalid_param", f"give between 1 and {MAX_SOURCES} sources")
    out = []
    roots: set[str] = set()
    namespaces: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise fail("invalid_param", "each source needs root, name, namespace and mode only")
        if any(not isinstance(v, str) or not v.strip() for v in source.values()):
            raise fail("invalid_param", "source fields must be non-empty strings")
        s = {k: v.strip() for k, v in source.items()}
        root = s["root"]
        if len(root) > 2048 or len(s["name"]) > 200:
            raise fail("invalid_param", "source path or name is too long")
        if len(root) >= 2 and root[0] in "/\\" and root[1] in "/\\":
            raise fail("invalid_param", "UNC and device paths are not supported")
        posix, windows = PurePosixPath(root), PureWindowsPath(root)
        if not (posix.is_absolute() or windows.is_absolute()):
            raise fail("invalid_param", "a source root must be an absolute local path")
        if root in (posix.anchor, windows.anchor) or ".." in posix.parts or ".." in windows.parts:
            raise fail("invalid_param", "use a prepared directory, not a filesystem root or '..'")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", s["namespace"]):
            raise fail("invalid_param", "use a distinct lowercase namespace for each source")
        if s["mode"] not in ("documents", "code"):
            raise fail("invalid_param", "mode must be documents or code")
        if root in roots or s["namespace"] in namespaces:
            raise fail("invalid_param", "source roots and namespaces must be distinct")
        roots.add(root)
        namespaces.add(s["namespace"])
        out.append(s)
    return {"sources": out}


def properties(source: dict[str, str]) -> dict[str, Any]:
    """Narrow native discovery without replacing its budgets or reader dispatch."""
    from kiro_crew.knowledge.ingestion import CODE_EXTS
    from kiro_crew.knowledge.readers import FileReader

    extensions = (
        {".md", ".pdf"} if source["mode"] == "documents" else CODE_EXTS & FileReader.SUPPORTED
    )
    return {
        "namespace": source["namespace"],
        "import_mode": source["mode"],
        "confine_to_root": True,
        "redact_secrets": True,
        "max_files": MAX_FILES,
        "extra_skip_dirs": [],
        "min_file_bytes": 0,
        "chunk_budget": None,
        "chunk_size": None,
        "chunk_overlap": None,
        "include_extensions": sorted(extensions),
        "ignore_patterns": list(IGNORE_PATTERNS),
    }


def preview(
    params: dict[str, Any],
    snapshot: dict[str, Any],
    out: dict[str, Any],
    *,
    fail: Callable[[str, str], Exception],
    step: Callable[..., dict[str, Any]],
) -> None:
    """Plan new sources and interrupted confirmations; never reset an existing scan."""
    entries = snapshot.get("sources")
    if not isinstance(entries, list) or len(entries) != len(params["sources"]):
        raise fail("knowledge_unavailable", "folder discovery is unavailable")
    plan, changes = [], []
    for source, entry in zip(params["sources"], entries):
        props = properties(source)
        existing = entry["existing"]
        if existing:
            # JSON comparison preserves the distinction between true and 1.
            if existing["source_type"] != "local_folder" or json.dumps(
                existing["properties"], sort_keys=True
            ) != json.dumps(props, sort_keys=True):
                raise fail("source_conflict", "an existing source has different scope or filters")
            status = existing["sync_status"]
            action = "Keep existing scan and file statuses"
            if status == "pending_confirmation":
                plan.append(
                    step("POST", f"/api/knowledge/sources/{quote(existing['id'], safe='')}/confirm")
                )
                action = "Confirm and start indexing"
        else:
            index = len(plan)
            plan.append(
                step(
                    "POST",
                    "/api/knowledge/sources",
                    {
                        "name": source["name"],
                        "source_type": "local_folder",
                        "uri": entry["uri"],
                        "namespace": source["namespace"],
                        "properties": props,
                    },
                )
            )
            plan.append(
                step(
                    "POST",
                    f"/api/knowledge/sources/{{{{step{index}.id}}}}/confirm",
                    fill=[
                        {
                            "field": "id",
                            "source": "step",
                            "step": index,
                            "key": "id",
                            "in": "path",
                        }
                    ],
                )
            )
            status = "not registered"
            action = "Register and start indexing"
        changes.extend(
            [
                {"label": source["name"], "after": entry["uri"]},
                {"label": "Action", "before": status, "after": action},
                {"label": "Namespace / mode", "after": f"{source['namespace']} / {source['mode']}"},
                {
                    "label": "Files / estimated chunks / model calls",
                    "after": f"{entry['files']} / {entry['estimated_chunks']} / {entry['estimated_llm_calls']}",
                },
                {
                    "label": "Chunks per sweep (0 = unbounded)",
                    "after": str(entry.get("chunk_budget_per_sweep", "configured default")),
                },
            ]
        )
    if not plan:
        raise fail(
            "no_change", "sources already registered; inspect their file statuses and retrieval"
        )
    changes.append(
        {
            "label": "Processing",
            "after": "Uses configured extraction and may consume credits. "
            "Detected secrets are masked. Verify indexing and retrieval separately.",
        }
    )
    out.update(
        title=f"Submit {len(entries)} prepared sources for indexing",
        changes=changes,
        scope=None,
        apply=plan,
    )
