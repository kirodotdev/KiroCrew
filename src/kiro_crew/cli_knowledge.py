"""The ``kirocrew knowledge``, ``kirocrew consolidate`` and ``kirocrew skills`` handlers.

``kiro_crew.cli.main`` dispatches these "Memory and knowledge" commands itself:
``_knowledge`` runs ``knowledge dedup`` and the read-only ``knowledge stats``
(``_knowledge_stats``), ``_consolidate_cmd`` forces history consolidation for one session
or for every session with unconsolidated messages, and ``_skills_cmd`` runs the operator's
``skills authority-retire`` maintenance on the auto-skill authority.

Split out of ``kiro_crew.cli``, which stays the import path and the patch target: every name
here is reached as ``kiro_crew.cli.<name>``, and a patch through that facade reaches this
module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.config.loader import build_provider_factory
from kiro_crew.gateway_lock import GatewayLock, GatewayLockError
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.knowledge import store as knowledge_store
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.memory import MemoryStore
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.skills import (
    SkillsLoader,
    initialize_gateway_auto_skill_private_authority,
    retire_auto_skill_private_authority,
)

# The facade's logger, by name: records from here carry ``kiro_crew.cli``.
logger = logging.getLogger("kiro_crew.cli")


def _knowledge_stats(args) -> None:
    """``kirocrew knowledge stats [--json]`` -- read-only counts, no repair verb."""

    db_path = config_dir() / "workspace" / "knowledge" / "knowledge.db"
    as_json = bool(getattr(args, "json", False))
    if not db_path.exists():
        sel().log_tool_invocation(
            session_key="cli", source="cli", tool_name="knowledge_stats", outcome="not_configured"
        )
        if as_json:
            print(json.dumps({"error": "not_configured"}))
        else:
            print("Knowledge Library is not configured (no knowledge.db). Ingest documents first.")
        return
    # Read-only means the FILE is opened read-only. A normal open runs the
    # schema migration, whose orphan sweep takes the writer lock and deletes
    # itemless source rows -- a write this verb must not make. The trade is
    # that a library behind this schema is reported, not repaired here.
    store = KnowledgeStore.open_read_only(str(db_path))
    try:
        stats = store.aggregate_stats()
    except knowledge_store.sqlite3.OperationalError as exc:
        if "no such" not in str(exc):
            raise
        sel().log_tool_invocation(
            session_key="cli", source="cli", tool_name="knowledge_stats", outcome="schema_behind"
        )
        if as_json:
            print(json.dumps({"error": "schema_behind", "detail": str(exc)}))
        else:
            print(
                f"Knowledge database is behind this schema ({exc}). Stats opens it "
                "read-only and does not migrate; start the gateway or run "
                "`kirocrew knowledge dedup --apply` once to migrate it, then retry."
            )
        return
    finally:
        store.db.close()
    sel().log_tool_invocation(
        session_key="cli",
        source="cli",
        tool_name="knowledge_stats",
        outcome="success",
        metadata={"sources": stats.sources, "documents": stats.documents, "items": stats.items},
    )
    if as_json:
        print(
            json.dumps(
                {
                    "sources": stats.sources,
                    "documents": stats.documents,
                    "items": stats.items,
                    "per_source": [
                        {
                            "id": s.source_id,
                            "name": s.name,
                            "documents": s.documents,
                            "items": s.items,
                        }
                        for s in stats.per_source
                    ],
                },
                indent=2,
            )
        )
        return
    print(
        f"Knowledge Library: {stats.sources} source(s), "
        f"{stats.documents} document(s), {stats.items} item(s)"
    )
    if not stats.per_source:
        return
    # Width from the data, so a long source name does not wrap the count columns.
    name_width = max(len(s.name) for s in stats.per_source)
    print(f"\n  {'SOURCE'.ljust(name_width)}  {'DOCS':>6}  {'ITEMS':>6}")
    for s in stats.per_source:
        print(f"  {s.name.ljust(name_width)}  {s.documents:>6}  {s.items:>6}")


def _knowledge(args) -> None:
    """``kirocrew knowledge dedup|stats`` -- duplicate collapse and read-only counts."""
    action = getattr(args, "knowledge_action", None)
    if action == "stats":
        _knowledge_stats(args)
        return
    if action != "dedup":
        print("Usage: kirocrew knowledge dedup [--apply] | kirocrew knowledge stats [--json]")
        return
    apply = bool(getattr(args, "apply", False))
    db_path = config_dir() / "workspace" / "knowledge" / "knowledge.db"
    if not db_path.exists():
        sel().log_tool_invocation(
            session_key="cli", source="cli", tool_name="knowledge_dedup", outcome="not_configured"
        )
        print("Knowledge Library is not configured (no knowledge.db). Ingest documents first.")
        return
    if apply:
        store = KnowledgeStore(str(db_path))
    else:
        # The dry run promises "no changes", and an ordinary open breaks that
        # promise before the sweep starts: the constructor runs the schema
        # migration, whose orphan reap deletes itemless source rows. So the
        # preview opens the file read-only (SQLite mode=ro) and only --apply
        # takes the migrating, writing open.
        store = KnowledgeStore.open_read_only(str(db_path))
    try:
        results = dedup_sweep(store, apply=apply)
    except knowledge_store.sqlite3.OperationalError as exc:
        if apply or "no such" not in str(exc):
            raise
        sel().log_tool_invocation(
            session_key="cli", source="cli", tool_name="knowledge_dedup", outcome="schema_behind"
        )
        print(
            f"Knowledge database is behind this schema ({exc}). The dry run opens it "
            "read-only and does not migrate; start the gateway or run "
            "`kirocrew knowledge dedup --apply` once to migrate it, then retry."
        )
        return
    finally:
        store.db.close()
    sel().log_tool_invocation(
        session_key="cli",
        source="cli",
        tool_name="knowledge_dedup",
        outcome="applied" if apply else "preview",
        metadata={"duplicate_count": len(results), "apply": apply},
    )
    mode = "APPLIED" if apply else "DRY RUN -- no changes; pass --apply to delete"
    if not results:
        print(f"[{mode}] No cross-source duplicate documents found.")
        return
    verb = "Deleted" if apply else "Would delete"
    print(f"[{mode}] {len(results)} duplicate document(s):\n")
    for r in results:
        print(f"  {verb}: {r['loser']}  ({r['items_deleted']} chunks)")
        print(f"      keep: {r['winner']}   [{r['reason']}]")


def _skills_cmd(args: argparse.Namespace) -> None:
    """Run operator maintenance for the auto-skill authority."""
    if getattr(args, "skills_action", None) != "authority-retire":
        print("Usage: kirocrew skills authority-retire")
        return
    try:
        with GatewayLock(config_dir()):
            retired_root, retired_record, retired_history = retire_auto_skill_private_authority()
    except (GatewayLockError, OSError, RuntimeError, ValueError) as exc:
        print(f"Error: auto-skill authority was not retired: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Auto-skill authority retired for inspection:")
    print(f"  root: {retired_root}")
    print(f"  record: {retired_record if retired_record is not None else '(none present)'}")
    for quarantine in retired_history or ("(none present)",):
        print(f"  quarantine: {quarantine}")
    print(
        "The public quarantines moved aside with the root, so a fresh authority starts "
        "with no claim history; nothing was deleted. To requeue a candidate left in one, "
        "move its <slug>--<token> directory back to skills/auto/.pending/<slug>."
    )
    print("By-name live mutations and pending dismissal can now resume on this data home.")


def _consolidate_cmd(args) -> None:
    """Force history consolidation (and auto-skill extraction) for sessions."""

    cfg = KiroCrewConfig.load()
    conv_log = ConversationLog()
    conv_log.init()

    session_key = args.session_key
    consolidate_all = getattr(args, "consolidate_all", False)

    if not session_key and not consolidate_all:
        # List path — lightweight, no heavy machinery needed
        found = []
        for f in conv_log._dir.glob("*.jsonl"):
            key = f.stem
            count = conv_log.unconsolidated_count(key)
            if count > 0:
                found.append((key, count))
        if not found:
            print("No sessions with unconsolidated messages.")
            return
        print(f"Sessions with unconsolidated messages ({len(found)}):\n")
        for key, count in sorted(found, key=lambda x: -x[1]):
            print(f"  {key}  ({count} messages)")
        print("\nRun with a session key or --all to consolidate.")
        return

    # A standalone CLI process does not pass through gateway startup, so attempt
    # the same certification here. As on the gateway, a refusal never stops the
    # command: the initializer records the reason, every staging and promotion
    # path refuses, and the consolidator skips skill detection for this run
    # while memory consolidation proceeds.
    try:
        initialize_gateway_auto_skill_private_authority()
    except Exception as exc:  # noqa: BLE001 -- auto-skills fail closed, consolidation does not
        if cfg.skills.auto_create_from_sessions:
            print(f"Auto-skill creation is off for this run: {exc}", file=sys.stderr)
        else:
            logger.debug("Auto-skill authority unavailable: %s", exc)

    # Heavy machinery only for actual consolidation
    mem = MemoryStore()
    mem.init()
    skills = SkillsLoader()
    sessions = SessionManager(cfg, provider_factory=build_provider_factory(cfg))

    # vector_store omitted: skill dedup uses SkillsLoader.find_similar() (Jaccard),
    # not vector_store. vector_store is for episodic memory embeddings only.
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=mem,
        sessions=sessions,
        skills_loader=skills,
        auto_skills_enabled=cfg.skills.auto_create_from_sessions,
        auto_refine_enabled=cfg.skills.auto_refine_on_deviation,
        auto_min_tool_calls=cfg.skills.auto_min_tool_calls,
        auto_similarity_threshold=cfg.skills.auto_similarity_threshold,
        approval_required=cfg.skills.approval_required,
        auto_apply_updates=cfg.skills.auto_apply_updates,
        max_auto_skills=cfg.skills.max_auto_skills,
        stale_after_days=cfg.skills.stale_after_days,
        archive_after_days=cfg.skills.archive_after_days,
        generate_scripts=cfg.skills.generate_scripts,
        judge_model=cfg.skills.judge_model,
    )

    async def _run(keys: list[str]) -> None:
        for key in keys:
            try:
                sel().log_api_access(
                    caller="cli",
                    operation="consolidate",
                    outcome="allowed",
                    source="cli",
                    resources=key,
                )
                count = conv_log.unconsolidated_count(key)
                if count < 1:
                    print(f"  {key}: no unconsolidated messages, skipping")
                    continue
                print(f"  {key}: consolidating {count} messages...")
                if not await consolidator.consolidate_now(key):
                    print(f"  {key}: no completed pass recorded")
                    continue
                # consolidate_now drains the tail over as many bounded passes as
                # it takes, but it can stop short — a backoff armed part-way
                # through, or a span it could not advance over. Report what the
                # transcript says rather than the call's success flag: this
                # process exits here, with no idle sweep behind it to finish a
                # remainder, so a bare "done" would be the last word on messages
                # nothing has read.
                left = conv_log.unconsolidated_count(key)
                if left:
                    print(f"  {key}: partially consolidated, {left} message(s) remain")
                else:
                    print(f"  {key}: done ✓")
            except Exception:
                logger.debug("consolidate (or SEL) failed for %s", key, exc_info=True)

    if consolidate_all:
        keys = [
            f.stem
            for f in conv_log._dir.glob("*.jsonl")
            if conv_log.unconsolidated_count(f.stem) > 0
        ]
        if not keys:
            print("No sessions with unconsolidated messages.")
            return
        print(f"Consolidating {len(keys)} session(s)...")
        asyncio.run(_run(keys))
    else:
        print(f"Consolidating session: {session_key}")
        asyncio.run(_run([session_key]))

    print("\nDone. Check ~/.kiro/crew/skills/auto/ for new skills.")
