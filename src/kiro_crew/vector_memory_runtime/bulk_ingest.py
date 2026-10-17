"""Bulk writers that feed the write paths: migration, import and promotion.

``migrate_from_markdown`` imports Global V1's ``lessons.jsonl`` and ``workspace/memory``
Markdown, ``import_memory`` replays an export, and ``promote_episodic_patterns`` turns a
cluster of repeated episodes into one semantic fact. ``vector_memory_runtime.migration``
parses and clusters; this module writes, and every write goes through the store's own write
methods.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same name
and take the store as ``self``. Store fields read: ``embed_fn`` and ``_memory_version``;
``_promotion_refused`` is read and updated.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from kiro_crew import memory_schema
from kiro_crew.vector_memory_runtime import migration as _migration

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def migrate_from_markdown(self: VectorMemoryStore) -> dict[str, int]:
    """Migrate Global V1 Markdown and JSONL learning into its vector store."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _EPISODIC_TEXT_MAX = vm._EPISODIC_TEXT_MAX
    _EPISODIC_TEXT_MIN = vm._EPISODIC_TEXT_MIN
    config_dir = vm.config_dir

    if self._memory_version == 2:
        raise ValueError("Member databases do not import legacy learned files")
    # Honor KIROCREW_HOME via config_dir() so the source directory matches
    # what legacy_memory_present() detects — hardcoding Path.home() would
    # migrate a different dir than was detected under a custom home, then
    # flip migrated=True having imported nothing (silent data loss).
    home = config_dir()
    base = home / "workspace" / "memory"
    counts = {"semantic": 0, "episodic": 0, "skipped": 0}
    # The three markdown sources below are memory SOURCE text, so they are read
    # through the injected file access rather than with Path methods -- if the
    # default workspace's memory tree is mounted elsewhere, this import has to
    # read the tree that is actually in use, not an abandoned local copy of it.
    #
    # lessons.jsonl is deliberately NOT read that way: it sits at the data-home
    # ROOT, outside the memory tree, so no memory mount covers it.
    from kiro_crew.memory_files import memory_files_for
    from kiro_crew.platform.interfaces import MemoryRoots

    files = memory_files_for(
        MemoryRoots(
            workspace=home / "workspace",
            memory_dir=base,
            history_dir=base / "history",
            memory_version=1,
        )
    )

    # ── Lessons ──
    lessons_path = home / "lessons.jsonl"
    if lessons_path.is_file():
        for lesson in _migration.legacy_lessons(lessons_path):
            if lesson is _migration.SKIP:
                counts["skipped"] += 1
                continue
            rule, category, negative, raw_scope, applies = lesson
            try:
                if rule and self.write_lesson(
                    rule,
                    category,
                    negative,
                    source="migration",
                    repo_scope=raw_scope,
                    applies=applies,
                ):
                    counts["semantic"] += 1
                else:
                    counts["skipped"] += 1
            except (json.JSONDecodeError, KeyError):
                counts["skipped"] += 1

    # ── Preferences ──
    prefs_path = base / "preferences.md"
    if files.exists(prefs_path):
        for text in _migration.preference_bullets(files, prefs_path):
            # Try smart key-value extraction
            parsed = self._parse_preference(text)
            if parsed:
                key, value = parsed
                if self.set_semantic(key, value, 0.85, "migration") is None:
                    counts["semantic"] += 1
                    continue
            # Fallback: write as episodic
            if self.write_episodic(
                text,
                embedding=self._try_embed(text),
                importance=0.6,
                source="migration",
                tags=["preference"],
            ):
                counts["episodic"] += 1
            else:
                counts["skipped"] += 1

    # ── Projects ──
    proj_path = base / "projects.md"
    if files.exists(proj_path):
        for kind, text, project in _migration.project_entries(files, proj_path):
            if kind == "project":
                if self.set_semantic("project.name", text, 0.85, "migration") is None:
                    counts["semantic"] += 1
                else:
                    counts["skipped"] += 1
            elif text and self.write_episodic(
                text,
                embedding=self._try_embed(text),
                importance=0.5,
                source="migration",
                tags=["project", project],
            ):
                counts["episodic"] += 1
            else:
                counts["skipped"] += 1

    # ── History ──
    history_dir = base / "history"
    for text in _migration.history_paragraphs(
        files, history_dir, min_chars=_EPISODIC_TEXT_MIN, max_chars=_EPISODIC_TEXT_MAX
    ):
        if self.write_episodic(
            text,
            embedding=self._try_embed(text),
            importance=0.4,
            source="migration",
            tags=["history"],
        ):
            counts["episodic"] += 1
        else:
            counts["skipped"] += 1

    embedded_row = self._fetch_one_locked(
        "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NOT NULL"
    )
    embedded_n = embedded_row[0] if embedded_row is not None else 0
    logger.info(
        "Migration complete: semantic=%d episodic=%d skipped=%d embedded=%d",
        counts["semantic"],
        counts["episodic"],
        counts["skipped"],
        embedded_n,
    )
    return counts


def import_memory(self: VectorMemoryStore, data: dict) -> dict[str, int]:
    """Import memory from an export dict with 'semantic' and 'episodic' arrays."""
    counts = {"semantic": 0, "episodic": 0, "skipped": 0}
    for entry in data.get("semantic", []):
        try:
            val = (
                json.loads(entry["value_json"])
                if isinstance(entry.get("value_json"), str)
                else entry.get("value")
            )
            conf = float(entry.get("confidence", 0.85))
            src = entry.get("source", "import")
            if self.set_semantic(entry["key"], val, conf, src) is None:
                counts["semantic"] += 1
            else:
                counts["skipped"] += 1
        except Exception:
            counts["skipped"] += 1
    for entry in data.get("episodic", []):
        try:
            if self.write_episodic(
                entry["text"],
                embedding=self._try_embed(entry["text"]),
                importance=float(entry.get("importance", 0.5)),
                source=entry.get("source", "import"),
                tags=(
                    json.loads(entry["tags"])
                    if isinstance(entry.get("tags"), str)
                    else entry.get("tags", [])
                ),
            ):
                counts["episodic"] += 1
            else:
                counts["skipped"] += 1
        except Exception:
            counts["skipped"] += 1
    return counts


def promote_episodic_patterns(
    self: VectorMemoryStore, min_count: int = 5, min_sim: float = 0.75
) -> int:
    """Scan episodic memories for repeated patterns and promote to semantic facts.

    Returns count of promoted entries.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _HAS_NUMPY = vm._HAS_NUMPY
    _MAX_PROMOTION_REFUSED = vm._MAX_PROMOTION_REFUSED

    if not self.embed_fn or not _HAS_NUMPY:
        logger.info("Promotion skipped: embeddings not available")
        return 0

    promoted = 0
    skipped = 0
    rows = self._fetch_all_locked(
        "SELECT id, text, embedding FROM episodic_memories "
        "WHERE is_deleted = 0 AND embedding IS NOT NULL "
        "ORDER BY importance DESC, created_at DESC LIMIT 500"
    )

    # Cluster similar episodic memories
    clusters = _migration.cluster_episodes(rows, min_sim)

    # Promote clusters with min_count+ members
    for members in clusters.values():
        if len(members) < min_count:
            continue
        canonical = max(members, key=lambda m: len(m["text"]))
        text = canonical["text"]

        key = self._infer_semantic_key(text)
        if not key:
            continue

        value = self._extract_value_from_text(text)
        # ``derived_from`` names the episode this fact was synthesized out of.
        # The cluster's own rows are tombstoned immediately below, so without it
        # the promoted fact is the only surviving trace and nothing records what
        # it came from -- the one provenance question a reader of a promoted row
        # actually asks. The canonical member is the representative the cluster
        # was collapsed onto.
        reject = self.set_semantic(
            key,
            value,
            0.9,
            "promotion",
            facets=memory_schema.MemoryFacets(derived_from=str(canonical["id"])),
        )
        if reject is None:
            promoted += 1
            for m in members:
                self._delete_episodic_row(m["id"])
            logger.info("Promoted %d episodic → %s: %s", len(members), key, value[:60])
        else:
            # A refused cluster keeps its rows and re-clusters identically next pass, so the
            # refusal repeats forever: count every pass, but warn only the first time per key.
            skipped += 1
            reject_code, reject_reason = reject
            # Keyed on the cause too: _infer_semantic_key returns a constant for every
            # "user prefers" cluster, so keying on key alone hides refusals of other causes.
            if (key, reject_code.value) not in self._promotion_refused:
                self._promotion_refused[(key, reject_code.value)] = None
                while len(self._promotion_refused) > _MAX_PROMOTION_REFUSED:
                    self._promotion_refused.popitem(last=False)
                logger.warning(
                    "Promotion skipped %s (%s: %s): %d rows retained, retried each pass",
                    key,
                    reject_code.value,
                    reject_reason,
                    len(members),
                )

    if skipped:
        logger.info("Promotion pass: %d promoted, %d skipped", promoted, skipped)
    return promoted
