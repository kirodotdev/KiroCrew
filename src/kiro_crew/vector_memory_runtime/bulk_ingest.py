"""Bulk writers that feed the write paths: migration, import and promotion.

``migrate_from_markdown`` imports Global V1's ``lessons.jsonl`` and ``workspace/memory``
Markdown, ``import_memory`` replays an export, and ``promote_episodic_patterns`` turns a
cluster of repeated episodes into one semantic fact. ``vector_memory_runtime.migration``
parses and clusters; this module writes, and every write goes through the store's own write
methods.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same name
and take the store as ``self``. Store fields read: ``embed_fn``, ``_memory_version``,
``_embedding_dim``, ``_space_generation`` and ``_confidence_threshold``;
``_promotion_refused`` is read and updated.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import struct
from typing import TYPE_CHECKING

from kiro_crew import memory_schema
from kiro_crew.vector_memory_runtime import migration as _migration
from kiro_crew.vector_memory_runtime.embedding import _EmbeddingVector

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def encode_embedding_blob(blob: object) -> str | None:
    """A stored vector BLOB as the base64 text an export carries, or None."""
    if isinstance(blob, (bytes, bytearray, memoryview)) and len(blob):
        return base64.b64encode(bytes(blob)).decode("ascii")
    return None


def export_embedding_space(recorded: str | None) -> tuple[str, dict | None]:
    """The space signature and ``{"model_id", "dim"}`` an export's vectors belong to.

    *recorded* is the store's ``recorded_embedding_space()``.

    An unrecorded space is the bundled model's, the only thing that could have
    written vectors before space tracking existed. The model is named only when
    this process can produce that exact signature from a model id and width --
    the bundled model, or the active backend (metadata only, no model load);
    otherwise it is None, and an importer carries none of the vectors.
    """
    from kiro_crew import embeddings

    signature = recorded or embeddings.default_embedding_space_signature()
    if signature == embeddings.default_embedding_space_signature():
        return signature, {"model_id": embeddings._MODEL_ID, "dim": embeddings._DEFAULT_DIM}
    try:
        backend = embeddings.get_shared_embedder()
        model_id, dim = str(backend.model_id), int(backend.dim)
    except Exception:
        logger.debug("Active embedding backend unavailable for export", exc_info=True)
        return signature, None
    if embeddings.embedding_space_signature(model_id, dim) == signature:
        return signature, {"model_id": model_id, "dim": dim}
    return signature, None


def _payload_space_carryable(
    data: dict, token: tuple[str | None, str], *, has_embedder: bool, dim: int
) -> bool:
    """Whether an import may persist the payload's own vectors on a store.

    *token* is the store's space token, read once by the caller and attached to
    every carried vector, so the space judged here is the one the commit checks.
    *has_embedder* says the store has an ``embed_fn``; *dim* is its vector width.

    Only where the store cannot re-derive them itself (no ``embed_fn``: the CLI
    store), and only when the payload names its model, that model produces the
    payload's signature, and the signature is the one this store's vectors and
    the active backend both use. A store with no recorded space counts as the
    bundled model's, as everywhere else, so a payload from a custom model lands
    NULL there for the gateway's sweep rather than being attributed to the
    wrong space.
    """
    if has_embedder:
        return False
    signature = data.get("embedding_space_sig")
    model = data.get("embedding_model")
    if not isinstance(signature, str) or not isinstance(model, dict):
        return False
    model_id, model_dim = model.get("model_id"), model.get("dim")
    if not isinstance(model_id, str) or not model_id or type(model_dim) is not int:
        return False
    if model_dim != dim:
        return False
    from kiro_crew import embeddings

    try:
        if embeddings.embedding_space_signature(model_id, dim) != signature:
            return False
        recorded = token[0] or embeddings.default_embedding_space_signature()
        if signature != recorded or signature != embeddings.active_embedding_space_signature():
            return False
        requested = embeddings.embedding_rebuild_generation()
    except Exception:
        logger.debug("Embedding space unavailable; import carries no vectors", exc_info=True)
        return False
    return not (requested and token[1] != requested)


def _decode_payload_vector(raw: object, dim: int) -> list[float] | None:
    """A base64 float32 vector of exactly *dim* finite values, else None."""
    if not isinstance(raw, str):
        return None
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) != 4 * dim:
        return None
    values = list(struct.unpack(f"{dim}f", blob))
    if not all(math.isfinite(v) for v in values):
        return None
    return values


# The confidence an imported semantic row with no ``confidence`` field is stored at.
IMPORT_CONFIDENCE_DEFAULT = 0.85


def _import_confidence(raw: object, replacement: float, *, writer: str = "memory import") -> float:
    """The ``0..1`` confidence an imported semantic row with *raw* is written at.

    *writer* names the write path in the warning line; member consolidation shares
    this bound for the confidence its model writes.

    An import file is the user's own data, so a bad confidence never skips its row:
    a number is clamped to ``0..1`` (an int is compared before it is converted, so one
    too large for a float clamps instead of raising), a string ``float()`` reads is
    read as before, and ``True``/``False`` keep 1.0/0.0. NaN, ``null`` and every other
    non-number take *replacement*, which the caller keeps at or above the store's
    confidence threshold: NaN passed that threshold before, so it still must. A
    replaced value logs a warning that never echoes it: a file can carry a 400-digit
    integer.
    """
    if isinstance(raw, bool):
        return float(raw)
    value: object = raw
    if isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            value = None
    if not isinstance(value, (int, float)) or (isinstance(value, float) and math.isnan(value)):
        logger.warning("%s: confidence is not a number; using %s", writer, replacement)
        return replacement
    clamped = max(0, min(1, value))
    if clamped != value:
        logger.warning("%s: confidence outside 0..1; using %s", writer, float(clamped))
    return float(clamped)


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
    """Import memory from an export dict with 'semantic' and 'episodic' arrays.

    A semantic row's exported vector is persisted as it is when
    ``_payload_space_carryable`` allows it under the store's space token, read
    once here before that check. Each carried vector holds that token, so the
    publish transaction drops it if a model swap lands at any point after. A row
    without a usable vector takes the normal write tail and, on a store with no
    embedder, waits for the gateway's sweep.
    """
    counts = {"semantic": 0, "episodic": 0, "skipped": 0}
    replacement = max(IMPORT_CONFIDENCE_DEFAULT, min(1.0, float(self._confidence_threshold)))
    token = self._embedding_token()
    generation = self._space_generation
    carry = _payload_space_carryable(
        data, token, has_embedder=self.embed_fn is not None, dim=self._embedding_dim
    )
    for entry in data.get("semantic", []):
        try:
            val = (
                json.loads(entry["value_json"])
                if isinstance(entry.get("value_json"), str)
                else entry.get("value")
            )
            conf = (
                _import_confidence(entry["confidence"], replacement)
                if "confidence" in entry
                else IMPORT_CONFIDENCE_DEFAULT
            )
            src = entry.get("source", "import")
            values = (
                _decode_payload_vector(entry.get("embedding"), self._embedding_dim)
                if carry
                else None
            )
            if values is None:
                reject = self.set_semantic(entry["key"], val, conf, src)
            else:
                reject = self.set_semantic(
                    entry["key"],
                    val,
                    conf,
                    src,
                    embedding=_EmbeddingVector(values, token),
                    embedding_resolved=True,
                    embedding_generation=generation,
                )
            if reject is None:
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
