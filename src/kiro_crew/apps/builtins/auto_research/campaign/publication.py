"""Documents a campaign publishes: the agent brief, the report and its exports.

- The brief (``brief.md``) the worker reads each cycle. It is written only after
  the row it renders commits, in commit order, under ``_brief_publish_lock``;
  a user-added question republishes it the same way.
- The report: ``FINDINGS.md`` read back for the dashboard, its LLM-authored
  HTML (with a mechanical fallback) and the reuse-or-create artifact export.
- The Knowledge Library export: a scrubbed copy of the findings, its dedup key,
  the source row, and the background ingestion that settles its sync status.

Every surface here is external, so content passes through ``untrusted`` first.
HTTP status decisions stay with the route adapters in ``handlers``.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.auto_research.campaign import LOGGER_NAME, storage, untrusted
from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

try:
    from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactStore

    _HAS_ARTIFACTS = True
except ImportError:
    _HAS_ARTIFACTS = False

logger = logging.getLogger(LOGGER_NAME)

_brief_publish_locks: dict[str, threading.Lock] = {}
_brief_publish_locks_guard = threading.Lock()


def _brief_publish_lock(campaign_id: str) -> threading.Lock:
    """Serialize one campaign's commit→brief-publish sequences (off-loop).

    ``brief.md`` must be published only AFTER the row it renders committed
    (a rollback must never leave a brief describing phantom state), and the
    publish order must match the commit order (a stale snapshot must never
    overwrite a newer brief). Holding this process-wide lock across
    ``BEGIN IMMEDIATE`` → ``commit()`` → ``_write_brief`` gives both: the DB
    write lock alone cannot, because it is released at commit, before the
    file write.
    """
    with _brief_publish_locks_guard:
        return _brief_publish_locks.setdefault(campaign_id, threading.Lock())


def _write_brief(cid: str, row: Any) -> None:
    """Write the campaign brief — question, scope, and the authoritative
    sub-question checklist the agent reads each cycle.

    Local file in the campaign dir (the agent's file-based interface) — not an
    external surface, so the user's own question text is written as-is.
    """
    subs = json.loads(row["sub_questions"] or "[]")
    srcs = json.loads(row["sources"] or "[]")
    cols = row.keys()
    constraints = (
        json.loads(row["scope_constraints"] or "[]") if "scope_constraints" in cols else []
    )
    lines = ["# Research Brief", "", f"**Question:** {row['question']}", ""]
    if constraints:
        lines += ["## Scope & Constraints", ""]
        lines += [
            f"- {c.get('q', '')} → {c.get('a', '')}" for c in constraints if isinstance(c, dict)
        ]
        lines.append("")
    if subs:
        lines.append(
            "**Sub-questions (authoritative checklist — answer each; do NOT invent your own "
            "initial set). Items tagged _(emergent)_ were discovered mid-research; items "
            "tagged _(user guidance)_ are directives the user added — follow them, even if "
            "phrased as an instruction rather than a question:**"
        )
        for s in subs:
            text = s.get("text", "") if isinstance(s, dict) else str(s)
            origin = s.get("origin", "grill") if isinstance(s, dict) else "grill"
            if origin == "emergent":
                tag = " _(emergent)_"
            elif origin == "manual":
                tag = " _(user guidance)_"
            else:
                tag = ""
            lines.append(f"- {text}{tag}")
    else:
        lines.append(
            "**Sub-questions:** (none provided — derive your own from the question and scope)"
        )
    lines += [
        "",
        f"**Sources allowed:** {', '.join(srcs) or 'any'}",
        f"**Max cycles:** {row['max_cycles']}",
    ]
    if not row["auto_approve"]:
        lines += [
            "",
            "**Questions allowed:** if the goal or scope is genuinely ambiguous in a "
            "way that would materially change your research direction, you MAY ask ONE "
            "high-leverage clarification question. Rules:\n"
            "- Only ask about DECISIONS the user must make — never ask about facts you "
            "can discover by exploring (filesystem, tools, code, web search).\n"
            "- Ask exactly ONE focused question per pause — multiple questions at once "
            "are bewildering and produce shallow answers.\n"
            "- First-principle: state what you know, the specific decision, and the "
            "options. Include your recommended answer.\n"
            "- Keep the bar high — proceed on a best-reasoned assumption for anything "
            "minor or self-resolvable.\n"
            "Write "
            '{"question": ..., "why": ..., "recommended": ...} to '
            "questions.json and end the turn — the campaign pauses for the user, who "
            "answers via Nudge.",
        ]
    if row["success_criteria"]:
        lines += [
            "",
            f"**Definition of Done:** {row['success_criteria']}",
            "Verify against this each cycle using your tools (run tests, review, eval); "
            "when met, set verification.passed=true in the finding.",
        ]
    lines += [
        "",
        "**Recursive exploration (emergent sub-questions):** As you research you will "
        "discover NEW high-value questions not in the initial list. Each cycle, in addition "
        "to your finding, you MAY propose follow-up sub-questions by writing "
        "`emergent_questions.json` in this dir as a JSON array: "
        '`[{"text": "...", "priority": 0.0-1.0}, ...]` where priority is how valuable '
        "/ relevant the lead is to the main question. The system ranks them, admits the top "
        "few per round (a budget), de-duplicates against existing questions, and appends the "
        "winners to the checklist above (tagged _(emergent)_) for you to investigate in "
        "later cycles — so you can follow leads BEYOND the initial questions. Do NOT "
        "re-propose questions already on the checklist, and stop proposing once the main "
        "question is sufficiently answered (your Definition of Done / verification).",
        "",
        "Each cycle, also read `guidance.txt` in this dir if present and follow any "
        "directive there (e.g. a FINALIZE MODE instruction to stop exploring and "
        "synthesize your final answer).",
        "",
        "**Ending the run:** if you decide the research is finished (goal met or no "
        "productive work remains), FIRST write `worker_done.json` in this dir as "
        '`{"reason": "<one line>"}` — this is the durable signal that you ended the '
        "run on purpose if the source stop record is unavailable — "
        "and only THEN call `autonudge_stop`.",
        "",
        "Adapt direction each cycle from prior findings; pursue the highest-value open "
        "lead toward the question.",
    ]
    # Parallel worker instruction
    pw = int(row["parallel_workers"]) if "parallel_workers" in row.keys() else 1
    if pw > 1:
        lines += [
            "",
            f"**Parallel execution:** You have {pw} parallel worker slots. Each cycle, "
            "use `spawn_run` with a `tasks` array to investigate up to "
            f"{pw} open sub-questions simultaneously (one task per sub-question). "
            "Each task should be a self-contained research instruction for that sub-question. "
            "Wait for all completion events, then synthesize results into your cycle finding. "
            f"If fewer than {pw} sub-questions remain open, spawn only as many as needed.",
        ]
    storage._campaign_dir(cid).joinpath("brief.md").write_text("\n".join(lines), encoding="utf-8")


def _append_question(cid: str, text: str) -> list | None:
    """Append a user-authored sub-question and republish the brief.

    Read-modify-write under one write transaction, publish after commit.
    ``BEGIN IMMEDIATE`` takes the write lock BEFORE the read, so two
    concurrent appends serialize instead of both reading the same base
    list and one overwriting the other's question. The publish lock spans
    commit→``_write_brief`` so the brief on disk always reflects a
    COMMITTED row and publish order matches commit order. Blocking; call
    off-loop. Returns the new checklist, or None for an unknown campaign.
    """
    with _brief_publish_lock(cid):
        db = storage._get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sub_questions, question, sources, scope_constraints, max_cycles, "
                "idle_secs, success_criteria, auto_approve FROM campaigns WHERE id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return None
            subs = json.loads(row["sub_questions"] or "[]")
            subs.append({"text": text, "origin": "manual", "status": "open"})
            db.execute(
                "UPDATE campaigns SET sub_questions = ? WHERE id = ?",
                (json.dumps(subs), cid),
            )
            # Re-read the row so _write_brief sees the updated sub_questions.
            # parallel_workers MUST be included — _write_brief defaults it to 1
            # when absent, which would silently drop the parallel instruction
            # from the brief.
            fresh = db.execute(
                "SELECT question, sub_questions, sources, scope_constraints, max_cycles, "
                "idle_secs, success_criteria, auto_approve, parallel_workers "
                "FROM campaigns WHERE id = ?",
                (cid,),
            ).fetchone()
            db.commit()
        finally:
            db.close()
        # Publish AFTER commit (rollback can never leave a phantom brief):
        # regenerate brief.md so the agent sees the new question next cycle.
        _write_brief(cid, fresh)
        return subs


def _read_report(campaign_id: str) -> str:
    """Read the agent's cumulative FINDINGS.md report (empty if none yet).

    UTF-8 with byte replacement: the report is LLM prose, and
    ``UnicodeDecodeError`` is a ``ValueError`` — NOT an ``OSError`` — so before
    this it escaped the handler and turned GET /campaigns/{id}/report into a 500
    for any report containing a non-ASCII character.
    """
    d = storage._safe_campaign_dir(campaign_id)
    if not d:
        return ""
    p = d / "FINDINGS.md"
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    except OSError:
        return ""


_REPORT_TIMEOUT = 90.0


def _build_report_prompt(question: str, subs: list, findings_md: str, total_cycles: int) -> str:
    """Prompt the LLM to author a polished, self-contained HTML report."""
    sub_lines = []
    for s in subs:
        if isinstance(s, dict):
            st = "answered" if s.get("status") == "answered" else "open"
            sub_lines.append(f"- [{st}] {s.get('text', '')}")
        else:
            sub_lines.append(f"- {s}")
    subs_block = "\n".join(sub_lines) if sub_lines else "(none)"
    return (
        "You are formatting a completed research campaign into a polished, "
        "self-contained HTML report for sharing.\n\n"
        f"{untrusted._UNTRUSTED_DATA_NOTICE}\n\n"
        f"# Research question\n{question}\n\n"
        f"# Sub-questions\n{subs_block}\n\n"
        f"# Cycles run\n{total_cycles}\n\n"
        "# Findings (markdown, authored during research)\n"
        f"{untrusted._fence_untrusted(findings_md)}\n\n"
        "Produce a SINGLE self-contained HTML document (no external assets) that "
        "presents this research clearly and attractively:\n"
        "- A header with the question and a one-paragraph executive summary you synthesize.\n"
        "- A 'Key findings' section highlighting the most important, well-evidenced points.\n"
        "- A 'Sub-questions' section showing which were answered vs still open.\n"
        "- Preserve any source citations / links present in the findings.\n"
        "- Use clean, modern inline CSS (system font, readable ~800px width, light theme).\n"
        "- Do NOT invent facts that are not present in the findings.\n"
        "Output ONLY the raw HTML document, starting with <!DOCTYPE html>. "
        "Do not wrap it in markdown code fences."
    )


def _render_findings_html(
    question: str, subs: list, findings_md: str, total_cycles: int, cid: str
) -> str:
    """Render campaign findings into a self-contained HTML document."""
    q = html_mod.escape(question)
    sub_items = ""
    for s in subs:
        text = html_mod.escape(s.get("text", "") if isinstance(s, dict) else str(s))
        origin = html_mod.escape(s.get("origin", "grill") if isinstance(s, dict) else "grill")
        status = s.get("status", "open") if isinstance(s, dict) else "open"
        icon = "✅" if status == "answered" else "🔍"
        sub_items += f"<li>{icon} {text} <em>({origin})</em></li>\n"
    # Convert markdown to basic HTML (just escape and preserve structure)
    body_html = html_mod.escape(findings_md).replace("\n\n", "</p><p>").replace("\n", "<br>")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Research: {q}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #1a1a1a; }}
h1 {{ font-size: 1.4em; }}
h2 {{ font-size: 1.1em; margin-top: 1.5em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }}
.meta {{ color: #666; font-size: 0.85em; }}
ul {{ padding-left: 1.5em; }}
li {{ margin: 0.3em 0; }}
.findings {{ background: #f9f9f9; padding: 1em; border-radius: 6px; margin-top: 1em; }}
p {{ margin: 0.5em 0; }}
</style></head><body>
<h1>🔬 {q}</h1>
<div class="meta">{total_cycles} cycles · Campaign {html_mod.escape(cid)}</div>
<h2>Sub-questions</h2>
<ul>{sub_items}</ul>
<h2>Findings</h2>
<div class="findings"><p>{body_html}</p></div>
</body></html>"""


def _read_report_slug(cid: str) -> sqlite3.Row | None:
    """The campaign's bound report-artifact slug row. Blocking; call off-loop."""
    db = storage._get_db()
    try:
        return db.execute(
            "SELECT report_artifact_slug FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
    finally:
        db.close()


def _live_report_slug(slug: str, cid: str) -> str | None:
    """``slug`` while its artifact still exists, so the UI never offers a dead
    link; a missing artifact or a broken store reads as no report."""
    try:
        ArtifactStore().get(slug)
    except ArtifactNotFoundError:
        return None
    except Exception:
        logger.exception("report-status lookup failed for %s", cid)
        return None
    return slug


def _read_export_row(cid: str) -> sqlite3.Row | None:
    """The row fields the artifact export renders. Blocking; call off-loop."""
    db = storage._get_db()
    try:
        return db.execute(
            "SELECT question, sub_questions, total_cycles, status, report_artifact_slug "
            "FROM campaigns WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        db.close()


async def _author_report_html(
    pool: Any, question: str, subs: list, findings_md: str, total_cycles: int, cid: str
) -> str:
    """The artifact export's HTML, scrubbed for a shareable surface.

    Prefers an LLM-authored report (synthesized + nicely formatted); when the
    pool is absent, fails or returns nothing it falls back to a mechanical
    render of FINDINGS.md, so the export never hard-fails. The findings fed to
    the prompt are capped so a huge report doesn't blow the context.
    """
    authored: str | None = None
    if pool is not None:
        try:
            prompt = _build_report_prompt(question, subs, findings_md[:24000], total_cycles)
            raw = (await pool.send(prompt, timeout=_REPORT_TIMEOUT)).strip()
            # LLMs often wrap HTML in a ```html … ``` fence despite instructions.
            raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            if raw:
                authored = raw
        except Exception:
            logger.exception("LLM report authoring failed for %s; using fallback", cid)
    # Graceful fallback: mechanical render of the (escaped) findings.
    html: str = (
        authored
        if authored is not None
        else _render_findings_html(question, subs, findings_md, total_cycles, cid)
    )
    # Redact agent/user-authored content before it lands in a shareable,
    # publishable artifact (HTML-escaping does NOT remove leaked credentials /
    # exfil URLs — that's this step). Applied uniformly to both paths.
    return untrusted._redact_finding({"v": html})["v"]


def _persist_report_slug(cid: str, slug: str) -> None:
    """Bind the report artifact to the campaign. Blocking; call off-loop."""
    db = storage._get_db()
    try:
        db.execute("UPDATE campaigns SET report_artifact_slug = ? WHERE id = ?", (slug, cid))
        db.commit()
    finally:
        db.close()


async def _publish_report_artifact(
    cid: str, question: str, html: str, existing_slug: str | None
) -> tuple[Any, str, bool]:
    """Reuse-or-create the report artifact: ``(artifact, name, regenerated)``.

    Repeated exports update ONE artifact (new version) instead of spawning a
    fresh duplicate on every click. A stored slug is reused only if the
    artifact still exists -- if the user deleted it, a fresh one is created and
    its slug re-bound, so the next export regenerates this same artifact and
    the UI can show "View report" upfront.
    """
    store = ArtifactStore()
    safe_q = untrusted._redact_finding({"v": question})["v"]
    name = f"Research: {safe_q[:50]}"
    art = None
    regenerated = False
    if existing_slug:
        try:
            store.get(existing_slug)  # existence probe
            art = store.update(
                existing_slug,
                content=html,
                name=name,
                description=f"Research findings for campaign {cid}",
                actor="agent",
                snapshot=True,
            )
            regenerated = True
        except ArtifactNotFoundError:
            art = None  # stored slug is dead — create a fresh one below
    if art is None:
        art = store.create(
            name=name,
            content=html,
            kind="html",
            source="subagent",
            description=f"Research findings for campaign {cid}",
            tags=["research"],
        )
    if art.slug != existing_slug:
        await asyncio.to_thread(_persist_report_slug, cid, art.slug)
    return art, name, regenerated


def _knowledge_copy(campaign_dir: Path) -> Path:
    """The scrubbed copy of FINDINGS.md that is ingested -- never the raw file."""
    return campaign_dir / "findings_for_knowledge.md"


def _knowledge_uri(campaign_dir: Path) -> str:
    """The Knowledge Library dedup key: the resolved path of the scrubbed copy.

    ``resolve()`` works before the copy is written (it is not, until the user
    adds it), so the status probe computes it with no filesystem side effects.
    """
    return str(_knowledge_copy(campaign_dir).resolve())


async def _write_knowledge_copy(campaign_dir: Path, raw_findings: str) -> str:
    """Write the scrubbed copy the Knowledge Library ingests; return its URI.

    The Knowledge Library is an external surface (content surfaces to users and
    agents via RAG/search), so credentials + exfil URLs are scrubbed first: the
    agent may have encountered secrets mid-research, and ingesting the raw file
    would leak them.
    """
    redacted = untrusted._redact_finding({"v": raw_findings})["v"]
    await asyncio.to_thread(storage._write_text, _knowledge_copy(campaign_dir), redacted)
    return _knowledge_uri(campaign_dir)


def _read_question_row(cid: str) -> sqlite3.Row | None:
    """The campaign question the source is named after. Blocking; call off-loop."""
    db = storage._get_db()
    try:
        return db.execute("SELECT question FROM campaigns WHERE id = ?", (cid,)).fetchone()
    finally:
        db.close()


def _knowledge_source_name(row: sqlite3.Row | None, cid: str) -> str:
    """The Knowledge Library source name, scrubbed like the content it names.

    The source name is metadata on the same external surface, matching the
    treatment the artifact export applies to its artifact name. The question is
    redacted WHOLE, then bounded: cutting first can split a credential at the
    60-char boundary into fragments no redaction regex matches.
    """
    return (
        f"Research: {untrusted._redact_finding({'v': row['question']})['v'][:60]}"
        if row
        else f"Research: {cid}"
    )


def _add_source_marked_syncing(store: Any, name: str, uri: str) -> str:
    """Register the findings as a Knowledge Library source, marked syncing.

    The store hands out one connection per thread, so all statement work for
    the request runs off-loop in a single worker: a lock wait on the store's
    busy timeout must stall a thread, never the event loop. ``add_source`` rides
    in the same call because the status UPDATE needs its ``sid`` on the same
    per-thread connection. Blocking; call off-loop.
    """
    new_sid = store.add_source(name=name, source_type="local_file", uri=uri, properties={})
    store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (new_sid,))
    store.db.commit()
    return new_sid


async def _ingest_findings(pipeline: Any, store: Any, uri: str, sid: str, cid: str) -> None:
    """Ingest the scrubbed copy and settle the source's sync status."""

    def _mark_synced() -> None:
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
        store.db.commit()

    def _mark_error() -> None:
        store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (sid,))
        store.db.commit()

    def _mark_pending() -> None:
        store.db.execute("UPDATE sources SET sync_status = 'pending' WHERE id = ?", (sid,))
        store.db.commit()

    try:
        # A user's one-shot import: the click is deliberate, and this route has
        # no budget of its own the way the watcher and artifact-sync sweeps do,
        # so it counts against the explicit-import chunk ceiling.
        await pipeline.ingest_file(uri, source_id=sid)
        await asyncio.to_thread(_mark_synced)
    except ImportChunkBudgetError as exc:
        # Transient, so not 'error': sync_all skips an errored source, which
        # would quiesce this one permanently over a window that clears in a
        # minute. The findings file stays on disk, so a retry has content to
        # re-read.
        logger.warning("Findings ingestion deferred by import budget for %s: %s", cid, exc)
        await asyncio.to_thread(_mark_pending)
    except Exception:
        logger.exception("Research findings ingestion failed for %s", cid)
        await asyncio.to_thread(_mark_error)
