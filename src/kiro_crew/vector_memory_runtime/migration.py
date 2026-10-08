"""Legacy and derived memory sources, read into what the store's writers accept.

``VectorMemoryStore.migrate_from_markdown`` imports Global V1's ``lessons.jsonl``
and ``workspace/memory`` Markdown, and ``promote_episodic_patterns`` turns a
cluster of repeated episodes into one semantic fact. Both keep their writes on the
store; this module only reads the sources: it parses legacy files in the order
the migration writes them (the Markdown through the ``MemoryFiles`` the store
resolves and passes in), clusters episode vectors, and infers a fact's key and
value from episode text. Nothing here opens the database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.lesson_validation import authored_lesson_applies
from kiro_crew.project_scope import scope_is_admissible

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import MemoryFiles

#: Yielded in place of a lessons.jsonl line the migration counts as skipped.
SKIP = None


def legacy_lessons(
    path: Path,
) -> Iterator[tuple[Any, Any, Any, Any, str | None, Any, Any] | None]:
    """``(rule, category, negative, repo_scope, applies, cites, cited_commit)`` per line.

    Yields :data:`SKIP` for a line that does not decode, and for one whose
    ``repo_scope`` is PRESENT but unusable: an absent scope means global, but a
    present unusable one means the row wanted a scope and cannot say which, and
    passing it to ``write_lesson`` would normalise it to None and inject the
    correction everywhere -- fail-open. Blank lines yield nothing.

    Carries the scope across, since dropping it would silently widen a
    repository-scoped correction into a global one, and the authored tier for the
    same reason in the same direction: an omitted ``applies`` reads as unstated,
    which every read path serves AS a standing rule, so dropping it promotes a row
    the user filed as a past finding into one injected in every session. The tier
    is normalized READ-safely (``authored_lesson_applies``, not the write path's
    raising form): this reads the file directly, so a hand-edited or
    future-schema value must migrate the row as unstated rather than abort.

    Carries the cited files and commit too, so a row that was anchored to code stays
    anchored; ``write_lesson`` normalizes both, so a malformed value arrives as none.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            rule = data.get("rule", "")
            negative = data.get("negative")
            raw_scope = data.get("repo_scope")
            if raw_scope is not None and not scope_is_admissible(raw_scope):
                yield SKIP
                continue
            applies = authored_lesson_applies(data.get("applies"))
            category = data.get("category", "knowledge")
        except (json.JSONDecodeError, KeyError):
            yield SKIP
            continue
        yield (
            rule,
            category,
            negative,
            raw_scope,
            applies,
            data.get("cites"),
            data.get("cited_commit"),
        )


def preference_bullets(files: MemoryFiles, path: Path) -> Iterator[str]:
    """The non-empty ``- `` bullet texts of ``preferences.md``, in file order."""
    for line in files.read_text(path).splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        text = line[2:].strip()
        if text:
            yield text


def parse_preference(text: str) -> tuple[str, str] | None:
    """Extract key-value from preference text with better heuristics."""
    # Pattern 1: "key: value"
    if ": " in text:
        k, v = text.split(": ", 1)
        key = "pref." + re.sub(r"[^a-z0-9]+", "_", k.strip().lower()).strip("_")
        return (key, v.strip())
    # Pattern 2: "My favorite X is Y"
    if match := re.match(r"(?:my )?favorite (\w+)(?: is)? (.+)", text, re.IGNORECASE):
        key = f"pref.favorite_{match.group(1).lower()}"
        return (key, match.group(2).strip())
    # Pattern 3: "I prefer X"
    if match := re.match(r"I prefer (.+)", text, re.IGNORECASE):
        return ("pref.general", match.group(1).strip())
    return None


def project_entries(files: MemoryFiles, path: Path) -> Iterator[tuple[str, str, str]]:
    """``("project", name, "")`` or ``("detail", text, project)`` per ``projects.md`` bullet.

    A ``- name: ...`` bullet opens a project; a later plain bullet is a detail of
    the most recent one. A plain bullet before any project yields nothing, and a
    detail bullet whose text is blank yields ``("detail", "", project)``, which the
    migration counts as skipped.
    """
    current_project = ""
    for line in files.read_text(path).splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            name = line[2:].split(":")[0].strip()
            current_project = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            yield "project", name, ""
        elif line.startswith("- ") and current_project:
            yield "detail", line[2:].strip(), current_project


def history_paragraphs(
    files: MemoryFiles, history_dir: Path, *, min_chars: int, max_chars: int
) -> Iterator[str]:
    """Timestamped paragraphs of every ``history/*.md``, oldest file first.

    Markdown headers, HTML comments and paragraphs shorter than *min_chars* are
    skipped; each paragraph is clipped to *max_chars*.
    """
    for md_file in sorted(files.glob(history_dir, "*.md")):
        # ``read_entry``, not ``read_text``: this read was lossy
        # (``errors="replace"``) because one corrupt day must not abort the
        # whole migration, and the strict reader raises. The guarded reader
        # keeps that robustness with a better failure: a day that cannot be
        # read wholly and validly is SKIPPED, rather than imported into the
        # vector store with replacement characters standing in for its text.
        content = files.read_entry(md_file).content
        # Split on timestamp-like paragraphs
        paragraphs = re.split(r"\n(?=\[[\d-]+)", content)
        for para in paragraphs:
            text = para.strip()
            # Skip markdown headers, HTML comments, short text
            if not text or text.startswith("#") or text.startswith("<!--"):
                continue
            if len(text) < min_chars:
                continue
            yield text[:max_chars]


def cluster_episodes(rows: list, min_sim: float) -> dict[int, list[dict]]:
    """Greedy single-pass clusters of episodic rows by stored-vector similarity.

    A row joins the FIRST existing cluster whose founding row it matches above
    *min_sim* (a bare dot product: episodic vectors are L2-normalized at write),
    and otherwise founds its own. Keyed by the founder's position in *rows*.
    """
    from kiro_crew import vector_memory  # circular import: the optional numpy seam lives there

    np = vector_memory.np
    clusters: dict[int, list[dict]] = {}
    for i, row in enumerate(rows):
        vec_i = np.frombuffer(row["embedding"], dtype=np.float32)
        found_cluster = False
        for cluster_id, members in clusters.items():
            vec_c = np.frombuffer(members[0]["embedding"], dtype=np.float32)
            sim = float(np.dot(vec_i, vec_c))
            if sim > min_sim:
                members.append(dict(row))
                found_cluster = True
                break
        if not found_cluster:
            clusters[i] = [dict(row)]
    return clusters


def infer_semantic_key(text: str) -> str | None:
    """Infer semantic key from episodic text."""
    if re.search(r"(user|i) (prefer|like|use)", text, re.IGNORECASE):
        return "pref.general"
    if match := re.search(r"project (\w+) uses? (\w+)", text, re.IGNORECASE):
        proj = re.sub(r"[^a-z0-9]+", "_", match.group(1).lower())
        return f"project.{proj}.tool"
    return None


def extract_value_from_text(text: str) -> str:
    """Extract value from episodic text."""
    text = re.sub(r"^(user|i) (prefer|like|use)s? ", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^project \w+ uses? ", "", text, flags=re.IGNORECASE)
    return text.strip()
