"""The memory graph the dashboard draws: every memory document as nodes and edges.

``_build_memory_graph`` turns the preferences, projects, semantic entries, lessons
and history into graph nodes and links each node to the projects it names;
``GET /api/memory/graph`` in the handlers module serves the result.

Served through :mod:`kiro_crew.dashboard.handlers.memory`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.dashboard.handlers._shared import _redact_memory_field


def _build_memory_graph(mem: Any, lessons: list) -> tuple[list[dict], list[dict]]:
    """Synchronous helper — safe to run in a thread."""
    import hashlib
    import re

    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: dict[str, str] = {}
    seen_ids: set[str] = set()

    def _id(prefix: str, label: str) -> str:
        return hashlib.md5(f"{prefix}:{label}".encode(), usedforsecurity=False).hexdigest()[:12]

    def _add(prefix: str, label: str, group: str, title: str = "") -> str:
        nid = _id(prefix, label)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label[:60], "group": group, "title": title or label})
            node_ids[f"{prefix}:{label}"] = nid
        return nid

    # --- Preferences ---
    try:
        pref_text = mem.read_preferences() or ""
        for line in pref_text.splitlines():
            line = line.strip().removeprefix("- ").strip()
            if line and not line.startswith("#") and not line.startswith("<!--") and len(line) > 5:
                _add("pref", line[:80], "preference", line)
    except Exception:
        pass

    # --- Projects ---
    try:
        proj_text = mem.read_projects() or ""
        current_project = ""
        for line in proj_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current_project = stripped[3:].strip()
                _add("proj", current_project, "project", current_project)
            elif stripped.startswith("- ") and current_project:
                detail = stripped[2:].strip()
                if len(detail) > 3:
                    detail_id = _add(
                        "proj_d", f"{current_project}: {detail[:60]}", "project", detail
                    )
                    proj_id = node_ids.get(f"proj:{current_project}")
                    if proj_id:
                        edges.append({"from": proj_id, "to": detail_id})
    except Exception:
        pass

    # --- Semantic Memory (vector store) ---
    vs = mem.vector_store
    if vs:
        try:
            for entry in vs.get_all_semantic():
                key = entry.get("key", "")
                # Lesson rows are rendered as prose by the lessons loop below;
                # adding them here too would show each lesson twice, once as a
                # dict repr of its stored mapping.
                if str(key).startswith("lesson."):
                    continue
                val = entry.get("value_json", "")
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                val_str = str(val) if not isinstance(val, str) else val
                _add("sem", key, "semantic", f"{key} = {val_str[:120]}")
        except Exception:
            pass

    # --- Lessons ---
    try:
        lessons_data = None
        try:
            lessons_data = vs.get_lessons() if vs else None
        except Exception:
            pass
        if lessons_data:
            # Deferred import: ``vector_memory`` pulls snowballstemmer plus the
            # optional numpy/faiss imports, and this helper is the handler's
            # only use of it, on one search path.
            from kiro_crew.vector_memory import _lesson_display_text

            for entry in lessons_data:
                rule = entry.get("value_json", "")
                if isinstance(rule, str):
                    try:
                        rule = json.loads(rule)
                    except Exception:
                        pass
                # Rendered text for either storage shape; fall back to str() so a
                # malformed row still surfaces in search rather than vanishing.
                text = _lesson_display_text(rule) or str(rule)
                _add("lesson", text[:80], "lesson", text)
        else:
            for le in lessons:
                _add("lesson", le.rule[:80], "lesson", le.rule)
    except Exception:
        pass

    # --- History (recent days only) ---
    try:
        hist = mem.read_recent_history(days=14) or ""
        for line in hist.splitlines():
            stripped = line.strip()
            m = re.match(r"^#{1,4}\s+(.+)", stripped)
            if m:
                raw = str(_redact_memory_field(m.group(1).strip()))
                _add("hist", raw[:80], "history", raw)
            elif stripped.startswith("[") and "]" in stripped and len(stripped) > 20:
                raw = str(_redact_memory_field(stripped))
                _add("hist", raw[:80], "history", raw[:200])
    except Exception:
        pass

    # --- Auto-detect edges by project-name mention ---
    # Match the project's SHORT name, not the FULL project header (e.g.
    # "Acme (Public)"): the full header almost never occurs verbatim inside
    # node titles (~0 edges across thousands of nodes, leaving the graph with no
    # structure to lay out). The short name (leading token with any
    # parenthetical qualifier stripped: "Acme (Public)" -> "acme",
    # "kiro-cli (Rust)" -> "kiro-cli") is what actually shows up in semantic
    # keys, lessons, and history lines.
    def _project_short_name(full: str) -> str:
        base = re.sub(r"\(.*?\)", "", full).strip()
        parts = base.split()
        return (parts[0] if parts else base).lower()

    # Generic words that would link to a large fraction of nodes if a project
    # were literally named after one ("Web", "App", "The …"); excluded so a
    # common short name can't turn the graph back into a hairball.
    edge_stopwords = {
        "the",
        "and",
        "for",
        "new",
        "web",
        "app",
        "api",
        "dev",
        "doc",
        "docs",
        "test",
        "tests",
        "main",
        "core",
        "misc",
        "todo",
        "wip",
        "old",
        "tmp",
    }
    project_matchers: list[tuple[str, str]] = []
    for k in node_ids:
        if k.startswith("proj:") and ":" not in k.split(":", 1)[1]:
            short = _project_short_name(k.split(":", 1)[1])
            # Require >=3 chars and not a generic stopword to avoid noisy links.
            if len(short) >= 3 and short not in edge_stopwords:
                project_matchers.append((node_ids[k], short))

    for n in nodes:
        if n["group"] in ("preference", "semantic", "lesson", "history"):
            title_lower = n["title"].lower()
            for proj_id, short in project_matchers:
                if n["id"] == proj_id:
                    continue
                if re.search(r"\b" + re.escape(short) + r"\b", title_lower):
                    edges.append({"from": n["id"], "to": proj_id})

    return nodes, edges
