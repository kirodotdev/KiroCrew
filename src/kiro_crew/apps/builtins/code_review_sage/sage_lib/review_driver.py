#!/usr/bin/env python3
"""Review driver — code-enforced two-stage review loop (design §3 component 3,
pain point #3 plus design-gate waste avoidance).

Neither the clean-session-per-change guarantee NOR the Phase 1 -> Phase 2 switch
is left to the LLM. This deterministic driver owns both:

  Stage 1 (gate)  — spawn an isolated Phase-1-ONLY session per change; it writes
                    a gate-only result record (phase1 + blast_radius).
  Phase switch    — the driver READS the recorded gate_verdict. Every usable
                    verdict (PASS, CONCERNS, BLOCK) proceeds to Phase 2: a design
                    BLOCK informs the ship decision but does NOT skip the code
                    review, so the author sees all issues in one pass.
  Stage 2 (deep)  — for any usable verdict: spawn a second isolated session that
                    runs the Phase 2 dimensions and augments the record with
                    findings.

Both stages run on a **reusable worker pool** (``sage_lib/review_pool.py``): a bounded
set of long-lived ``AcpClient`` sessions, NOT a fresh ``/api/spawn`` sub-agent
per change. The driver hands each task to the pool via an injected ``dispatch``
callable and the call returns when that task's session finishes its turn (i.e.
the result record is on disk) — so there is no done-flag polling, no lingering
worker, and no reaper. Because pool workers are direct ACP sessions they bypass
the SubagentManager entirely: no agent card, no ``:lock:`` approval prompt, no
Slack relay — the review runs silently. Each reused worker is reset to a clean
conversation between CRs so reviews never cross-contaminate.

The driver then builds the Focus Report deterministically. The orchestrating
session cannot review inline because the driver owns the dispatch. The per-change
*judgment* (the gate verdict and the findings) still runs in each isolated worker
session using the code-review-sage ruleset — Python enforces the structure and
the phase switch, not the verdict itself.

Usage:
    python3 sage_lib/review_driver.py run --changes "<pr-url>[,<pr-url>...]" [--concurrency 3]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Optional KiroCrew runtime dep (absent when running standalone / in tests).
# Kept at module top per the imports guideline; guarded at each use site.
try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/review_driver.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import pipeline, report, results, review_pool, store  # noqa: E402


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs from LLM-generated text before it is
    posted to an external surface (the dashboard artifact store). No-op when the
    KiroCrew redaction lib isn't importable (standalone)."""
    if redact_exfiltration_urls is None or redact_credentials is None:
        return text
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


DEFAULT_TASK_TIMEOUT = 1800      # seconds per review task (gate or deep)
DEFAULT_MAX_REVIEW_ROUNDS = 3    # Phase-2 convergence rounds per change (config override)
_REPORT_ARTIFACT_TAG = "sage-report"   # tags every per-run report artifact
DEFAULT_REPORT_RETENTION = 20    # keep the N most-recent report artifacts; prune older


def _api_request(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    """Authenticated loopback call to the gateway API. Never raises."""
    base, secret = _gateway_base(), _local_secret()
    if not secret:
        return {"error": "gateway IPC secret unavailable"}
    headers = {"X-Internal-Secret": secret}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)}


def _prune_old_reports(keep: int) -> None:
    """Best-effort: keep only the N most-recent report artifacts (by updated_at);
    delete older ones so the artifact list doesn't grow unbounded."""
    lst = _api_request("GET", "/api/artifacts?tag=" + _REPORT_ARTIFACT_TAG)
    items = lst.get("artifacts") if isinstance(lst, dict) else None
    if not items:
        return
    items = sorted(items, key=lambda a: a.get("updated_at", ""), reverse=True)
    for a in items[max(0, keep):]:
        slug = a.get("slug")
        if slug:
            _api_request("DELETE", "/api/artifacts/" + slug)


def _archive_report(html_body: str, root: Path | None = None) -> str | None:
    """Create a NEW report artifact for this run (one per run, not versions of a
    single artifact) and prune old ones. Returns the new slug, or None on failure."""
    html_body = _redact(html_body)  # scrub LLM output before posting to the dashboard
    ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    slug = "sage-report-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    d = _api_request("POST", "/api/artifacts", {
        "name": "Code Review Sage Report — " + ts,
        "content": html_body, "kind": "widget",
        "tags": ["cr", _REPORT_ARTIFACT_TAG],
        "slug": slug,
    })
    if d.get("error"):
        return None
    new_slug = d.get("slug") or slug
    _prune_old_reports(DEFAULT_REPORT_RETENTION)
    return new_slug


def _default_archiver(html_body: str, root: Path | None = None) -> str | None:
    return _archive_report(html_body, root)


def _resolve_concurrency(explicit: int | None = None) -> int:
    """Effective driver fan-out: an explicit value wins; otherwise default to the
    worker pool's concurrency cap.

    Pool workers are direct ACP sessions (NOT ``/api/spawn`` sub-agents), so the
    gateway sub-agent cap no longer applies — ``review_pool.MAX_CONCURRENT`` is the
    single source of truth for how many reviews run at once. The pool also hard-caps
    concurrency itself, so this only governs how many tasks the driver offers it."""
    if explicit and explicit > 0:
        return max(1, int(explicit))
    return max(1, review_pool.MAX_CONCURRENT)


def _max_review_rounds() -> int:
    """Max Phase-2 convergence rounds per change (config: ``review.max_review_rounds``,
    default 3). The driver re-runs the deep review — feeding prior findings forward —
    until a round adds no new findings or this cap is hit, maximizing first-pass
    recall so issues don't drip out across later revisions. Clamped to >= 1 so at
    least one deep review always runs."""
    try:
        cfg = store.load_config()
        val = int((cfg.get("review") or {}).get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS))
    except Exception:  # pragma: no cover - defensive (bad/missing config)
        val = DEFAULT_MAX_REVIEW_ROUNDS
    return max(1, val)


def _cid(link: str) -> str:
    """Derive the change id from a GitHub PR link — filesystem-safe. A PR URL ->
    ``GH-<owner>-<repo>-<n>`` (matching the id ``adapters.parse_github_payload``
    records, so the worker's written record and the driver's read hit the same
    file); otherwise a sanitized fallback (never a raw URL, which is not a valid
    filename)."""
    try:
        owner, repo, number = pipeline.adapters.github_pr_parts(link)
        return pipeline.adapters.github_change_id(owner, repo, number)
    except pipeline.adapters.AdapterParseError:
        return results.safe_change_id(link)


def change_id_for(link: str) -> str:
    """Public alias for the change-id derivation. The app backend uses this to
    store the SAME key the driver writes progress under on the run record, so the
    dashboard can align each row with its live phase (queued/gating/deep/done/failed)
    and render a human label. Keeping this in one place prevents the frontend from
    re-deriving the id (and drifting from the backend's sanitization, e.g. an owner
    hyphen becoming an underscore)."""
    return _cid(link)


def _fetch_instruction(link: str) -> str:
    """Platform-aware FETCH instruction for the gate/deep prompts (GitHub only)."""
    try:
        platform = pipeline.adapters.detect_platform(link)
    except Exception:  # pragma: no cover - defensive (empty/odd link)
        platform = "github"
    return pipeline.fetch_spec(platform)


def build_gate_task(change_link: str) -> str:
    """Stage 1 prompt: Phase-1 design gate ONLY. The isolated session fetches the
    change, runs the gate, writes a GATE-ONLY result record (phase1 +
    blast_radius, deep_reviewed=false), and STOPS. It never runs Phase 2 — the
    driver reads the recorded verdict and decides whether Phase 2 happens."""
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "Run ONLY the Phase 1 design gate for EXACTLY ONE change: " + change_link + ".\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns from active namespaces "
        "(`python3 sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any).\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `python3 sage_lib/pipeline.py prepare --link " + change_link
        + " --payload-file <file>`.\n"
        "  4. Run the Phase 1 design gate ONLY -> gate_verdict (PASS|CONCERNS|BLOCK) "
        "+ design_risk + criticality. THINK DEEPLY and deliberately — this is the "
        "highest-leverage step and you are running at maximum thinking effort. Work the "
        "change through the skill's `Deep design reasoning` lenses (architectural fit, "
        "contract/data evolution, alternatives & proportionality, failure modes, "
        "root-cause vs symptom), each as a consequence chain, BEFORE settling on a "
        "verdict; the weakest applicable lens sets design_risk. BLOCK is ONLY for a "
        "genuine DESIGN defect (no real "
        "problem, wrong/over-engineered fix, or a clearly better alternative ignored); a "
        "large blast radius / high criticality is NEVER on its own a BLOCK — it means PASS "
        "or CONCERNS and review more deeply in Phase 2. Capture the design reasoning as a "
        "CHAIN OF "
        "THOUGHT in structured fields. design_headline: a STRAIGHTFORWARD, DIRECT "
        "description of the design issue AND the recommended direction — this is what "
        "the author actually reads, so get straight to the point with no preamble or "
        "hedging. Keep it tight (one or a few sentences — as long as the issue needs, "
        "not artificially capped), self-contained and actionable. Set it "
        "ONLY on a CONCERNS or BLOCK verdict; leave it empty on PASS. problem: the "
        "customer/system problem "
        "in one sentence. why_it_matters: one or two SHORT lines (who is hurt, how "
        "often/badly). solution_assessment: a few SHORT facets on SEPARATE LINES "
        "(NOT one paragraph — the report renders each line on its own so it must be "
        "scannable), each a 'Label: text' point, e.g. 'Resolution: does it fix the "
        "root cause?' / 'Mechanism: cause -> mechanism -> consequence' / 'Tradeoffs: "
        "side effects or sub-optimal choices' / 'Alternatives: a clearly better option "
        "ignored?'. Put a REAL newline between facets and omit any facet that does "
        "not apply.\n"
        "  5. Write a GATE-ONLY result record to data/results/<id>.json: phase1 "
        "(gate_verdict, design_risk, criticality, design_headline, problem, why_it_matters, "
        "solution_assessment) + blast_radius, deep_reviewed=false, empty findings, "
        "counts {red:0,yellow:0} (findings contract). Always review — do NOT skip on "
        "any prior result; re-reviewing the same change is expected.\n"
        "STOP after writing the gate record. Do NOT run Phase 2, do NOT post comments, "
        "do NOT spawn further subagents. Execute; do not ask questions."
    )


def build_deep_review_task(change_link: str) -> str:
    """Stage 2 prompt: Phase-2 deep review. Spawned by the driver ONLY when the
    recorded gate verdict is not BLOCK. The session RECORDS findings into the gate
    record — it does NOT post anything. The driver then builds Python-redacted
    comment bodies and a separate poster publishes them (security-controls: LLM
    output is redacted in Python before it can reach the CR surface)."""
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "The Phase 1 design gate for this change has ALREADY RUN and its verdict "
        "(PASS, CONCERNS, or BLOCK) is recorded. Run the Phase 2 deep review "
        "REGARDLESS of that verdict — a design BLOCK informs the ship decision but "
        "does NOT skip the code review. Review EXACTLY ONE change: " + change_link + ".\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns from active namespaces "
        "(`python3 sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any) and apply it as additional rules.\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `python3 sage_lib/pipeline.py prepare --link " + change_link
        + " --payload-file <file>`. Read the existing gate record and "
        "PRESERVE its phase1 verdict / criticality / rationale.\n"
        "  4. Phase 2: the 9 code-level dimensions + self-critique "
        "(Filter/Merge/Sharpen/Stabilize) -> surviving 🔴/🟡 findings. Assign "
        "severity per the three-tier rule: 🔴 must-fix = breaks now OR a latent "
        "issue with high probability AND high impact of failing soon (a 'have-to-fix' "
        "— do NOT downgrade it to 🟡 just because it works today); 🟡 should-fix = "
        "real but non-blocking; drop nice-to-haves. Keep these first-class: STRICT "
        "bidirectional description<->diff fidelity (no phantom claims, no undocumented "
        "change), and an explicit threat chain on every security finding (entry point "
        "-> trust boundary -> exploit -> impact).\n"
        "  5. RECORD ONLY — do NOT post any comments. Update the result record "
        "data/results/<id>.json: keep the gate's phase1 block; add `findings` (each "
        "with file, line, severity 🔴/🟡, dimension, observation, consequence, "
        "suggestion, snippet, lang); set `counts` {red,yellow}; set `ship_summary` to "
        "ONE straightforward line (good to ship + reason when there are no 🔴, or "
        "not-ready + the must-fix reason when there is a 🔴 or a design BLOCK); set "
        "deep_reviewed=true. The driver builds the redacted comment bodies and a "
        "separate poster publishes them — you MUST NOT call any comment tool.\n"
        "  6. If this change is itself a FIX (is_fix), run INLINE miss-analysis "
        "(learn-from-sage): trace the introducing change, ask which dimension was blind, "
        "and STAGE the learning into the candidate file "
        "(`python3 sage_lib/learning.py stage --file <pattern.json> --source fix_introduce`). "
        "It is NOT applied to the live ruleset until a human triggers consolidation.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )


def build_deep_followup_task(change_link: str) -> str:
    """Follow-up deep-review round (convergence loop). A prior round already
    RECORDED findings into the result record; this round hunts for ADDITIONAL
    issues the earlier round missed and APPENDS only net-new findings — it never
    repeats, rewords, or removes existing ones. The driver stops looping when a
    round adds nothing new (or hits the configured cap), maximizing first-pass
    recall so issues don't drip out across later revisions."""
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "This is a FOLLOW-UP review round for EXACTLY ONE change: " + change_link + ". "
        "A previous round already recorded findings into data/results/<id>.json.\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns from active namespaces "
        "(`python3 sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any) and apply it as additional rules.\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `python3 sage_lib/pipeline.py prepare --link " + change_link
        + " --payload-file <file>`. READ the existing result record and "
        "its current `findings` list.\n"
        "  4. Hunt for ADDITIONAL issues the earlier round MISSED: per the skill's "
        "Coverage mandate, walk EVERY changed hunk against ALL 9 dimensions. Apply the "
        "same three-tier severity rule (🔴 must-fix incl. latent 'have-to-fix', 🟡 "
        "should-fix, drop nice-to-haves) and the STRICT description<->diff fidelity + "
        "security threat-chain checks.\n"
        "  5. RECORD ONLY — do NOT post any comments. APPEND only NET-NEW findings to "
        "the existing `findings` (do NOT repeat, reword, or remove any already-recorded "
        "finding); recompute `counts` {red,yellow} over the FULL list; refresh "
        "`ship_summary`; keep deep_reviewed=true and PRESERVE the phase1 block. You "
        "MUST NOT call any comment tool.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )


def build_post_task(change_link: str) -> str:
    """Poster prompt: publish the driver-built, Python-REDACTED DRAFT comments for
    one change. The bodies are authoritative and already scrubbed in Python — the
    poster posts them VERBATIM and only resolves the (non-sensitive) anchor. This
    is what makes PR-surface redaction deterministic (security-controls): no LLM
    free-text reaches the PR, because the LLM never composes a posted body."""
    _preamble = (
        "You are a Code Review Sage poster running in an ISOLATED, CLEAN session. "
        "Your ONLY job: publish pre-built, pre-redacted DRAFT review comments for "
        "EXACTLY ONE change: " + change_link + ". The comment bodies are AUTHORITATIVE "
        "and already redacted in Python — post each one VERBATIM. Do NOT compose, edit, "
        "summarize, truncate, translate, or add to any body.\n"
    )
    # GitHub's draft is a PENDING review: ONE API call carrying all inline
    # comments + a body, created WITHOUT an `event` key so it is NOT submitted.
    # The envelope is pre-built + redacted in Python (`github_review_payload`);
    # the poster posts it verbatim and never submits. A HUMAN submits it.
    return (
        _preamble
        + "  1. Read data/results/<id>.json and take its `github_review_payload` "
        "object (fields: body, comments[], optional commit_id). It was assembled "
        "AND redacted in Python — use it EXACTLY as given; do NOT rebuild it. Parse "
        "<owner>/<repo>/<number> from the PR URL.\n"
        "  2. FIRST clear any stale sage draft: GitHub allows only ONE pending "
        "review per PR per user, so a leftover one would make step 3 fail with 422. "
        "GET repos/<owner>/<repo>/pulls/<number>/reviews and, if a review with "
        "state==\"PENDING\" exists WHOSE BODY CONTAINS the exact marker "
        "`[code-review-sage]`, DELETE just that one (DELETE "
        "repos/<owner>/<repo>/pulls/<number>/reviews/<review_id>) — it is a stale "
        "sage draft. NEVER delete a non-PENDING review or a PENDING review lacking "
        "that marker (it may be a human's in-progress draft).\n"
        "  3. THEN write `github_review_payload` to a temp JSON file and create ONE "
        "PENDING (unsubmitted) review:\n"
        "     gh api --method POST repos/<owner>/<repo>/pulls/<number>/reviews "
        "--input <tmpfile>\n"
        "     The payload has NO `event` key, so GitHub creates the review as "
        "PENDING — it is NOT submitted and only YOU can see it until a HUMAN "
        "submits it in the GitHub UI. You MUST NOT add an `event` field, MUST NOT "
        "call any submit/approve/dismiss endpoint, and MUST NOT run `gh pr review` "
        "(that would submit immediately). `gh` uses its own stored auth — never "
        "read, print, or pass any token.\n"
        "  4. Update data/results/<id>.json: set posted_comments = len(comments) "
        "plus 1 when `body` is non-empty; set design_comment_posted = true when "
        "`body` is non-empty (else false). Do NOT modify findings, phase1, "
        "pending_comments, or github_review_payload.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )


_RESOLVED_BASE: str | None = None


def _candidate_ports() -> list[int]:
    """Ports to try for the live gateway: KIROCREW_PORT, config.json dashboard.url,
    then the common gateway range (the gateway may be on 5477+ if 5476 was taken)."""
    out: list[int] = []

    def _add(v) -> None:
        try:
            p = int(v)
        except (TypeError, ValueError):
            return
        if 1 <= p <= 65535 and p not in out:
            out.append(p)

    _add(os.environ.get("KIROCREW_PORT"))
    try:
        home = os.environ.get("KIROCREW_HOME") or os.path.expanduser("~/.kirocrew")
        cfg = Path(home) / "config.json"
        if cfg.exists():
            _d = json.loads(cfg.read_text(encoding="utf-8")).get("dashboard") or {}
            url = _d.get("url") or ""
            m = re.search(r":(\d+)", url)
            if m:
                _add(m.group(1))
    except Exception:
        pass
    for p in (5476, 5477, 5478, 5479, 5480, 5486):
        _add(p)
    return out


def _probe(base: str, secret: str) -> bool:
    """True if a KiroCrew gateway is listening at base (any HTTP response, incl.
    401/404, means it's there; only connection errors mean it isn't)."""
    try:
        req = urllib.request.Request(base + "/api/spawn",
                                     headers={"X-Internal-Secret": secret} if secret else {})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status < 500
    except urllib.error.HTTPError:
        return True   # a gateway responded (e.g. 401/404) — it's the right port
    except Exception:
        return False


def _gateway_base() -> str:
    """Resolve the LIVE gateway base URL by probing candidate ports (cached). The
    gateway may not run on 5476 and config.json dashboard.url is often empty, so a
    blind default sends spawns to a dead port — probing finds the real one."""
    global _RESOLVED_BASE
    if _RESOLVED_BASE:
        return _RESOLVED_BASE
    secret = _local_secret()
    ports = _candidate_ports()
    for port in ports:
        base = f"http://localhost:{port}"
        if _probe(base, secret):
            _RESOLVED_BASE = base
            return base
    # best guess; the request will error clearly if wrong
    return f"http://localhost:{ports[0] if ports else '5476'}"


def _local_secret() -> str:
    """Read the gateway IPC secret (same mechanism the MCP server uses)."""
    home = os.environ.get("KIROCREW_HOME") or os.path.expanduser("~/.kirocrew")
    try:
        return (Path(home) / ".local_secret").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _unconfigured_dispatch(task: str, timeout: int = DEFAULT_TASK_TIMEOUT) -> dict:
    """Fallback when no pool dispatch was injected. The app backend always wires
    a real dispatch (``review_pool.make_sync_dispatch``); this only fires for a
    misconfigured/standalone call, and fails loudly rather than silently spawning.
    """
    return {
        "ok": False, "output": "",
        "error": "review pool dispatch not configured (no worker pool wired into run_review)",
    }


def run_review(changes: list[str], *, dispatch=None, archiver=_default_archiver,
               concurrency: int = 0, timeout: int = DEFAULT_TASK_TIMEOUT,
               generate_report: bool = True, root: Path | None = None,
               progress=None) -> dict:
    """Two-stage per change (bounded concurrency): a Phase-1 gate task, then a
    Phase-2 deep-review task for every usable verdict (PASS / CONCERNS / BLOCK).
    Each task is dispatched to the reusable worker pool (``dispatch``) and the
    call returns when that task's session finishes its turn. The driver reads
    the gate verdict; a BLOCK no longer skips Phase 2 (it only informs the ship
    decision), then builds the Focus Report. Returns a deterministic summary.

    ``dispatch`` is an injected ``(task, timeout) -> {ok, output, error}`` callable
    (the app backend wires ``review_pool.make_sync_dispatch``; tests inject a fake).
    ``concurrency`` <= 0 means auto: default to the worker pool's concurrency
    cap (``review_pool.MAX_CONCURRENT``)."""
    store.ensure_layout(root)
    changes = [c for c in changes if c]
    if not changes:
        return {"ok": False, "error": "no changes to review", "spawned": 0}
    dispatch = dispatch or _unconfigured_dispatch
    progress = progress or (lambda *a, **k: None)   # (change_id, phase, extra) sink

    # Clean slate for this run: clear the previous run's displayed report and any
    # leftover result records, so a new review never shows confusing prior-run
    # data. The previous report is already archived as an artifact (history kept).
    report.reset(root)
    results.clear_results(root)

    # Mark everything queued upfront so the page renders all rows at once.
    for _link in changes:
        progress(_cid(_link), "queued", {})

    concurrency = _resolve_concurrency(concurrency)
    per_change: list[dict] = []

    def _post_pending(change_id: str, link: str) -> dict:
        """Build the DRAFT comment bodies from the recorded findings + the always-on
        ship-readiness comment, REDACTING each in Python (pipeline.build_pending_comments
        -> _redact), persist them into the record, then dispatch the verbatim poster.
        Redaction is deterministic HERE — no LLM free-text reaches the CR. Returns
        posting stats. No poster is spawned when there is nothing to post."""
        cur = results.read_result(change_id, root) or {}
        pending = pipeline.build_pending_comments(cur)
        if not pending:
            return {"post_ok": True, "posted_comments": 0,
                    "design_comment_posted": False, "pending": 0}
        cur["pending_comments"] = pending
        # GitHub posts a single PENDING review, so assemble the deterministic,
        # already-redacted envelope in Python here — the poster posts it verbatim
        # via one `gh api` call and never composes bodies.
        try:
            _platform = pipeline.adapters.detect_platform(link)
        except Exception:  # pragma: no cover - defensive
            _platform = "github"
        if _platform == "github":
            cur["github_review_payload"] = pipeline.build_github_review_payload(cur)
        results.write_result(cur, root)
        spawn = dispatch(build_post_task(link), timeout)
        after = results.read_result(change_id, root) or {}
        return {
            "post_ok": spawn.get("ok", False),
            "post_error": spawn.get("error", ""),
            "posted_comments": int(after.get("posted_comments", 0) or 0),
            "design_comment_posted": bool(after.get("design_comment_posted")),
            "pending": len(pending),
        }

    def _one(link: str) -> dict:
        change_id = _cid(link)

        # --- Stage 1: Phase-1 design gate (cheap) ---
        progress(change_id, "gating", {})           # worker is running Phase 1 now
        gate_spawn = dispatch(build_gate_task(link), timeout)
        gate_rec = results.read_result(change_id, root)
        verdict = str(((gate_rec or {}).get("phase1") or {}).get("gate_verdict", "")).upper()

        rec: dict = {
            "change": link, "change_id": change_id,
            "gate_spawn_ok": gate_spawn.get("ok", False),
            "gate_error": gate_spawn.get("error", ""),
            "gate_verdict": verdict or "UNKNOWN",
            "phase2_ran": False,
            "deep_spawn_ok": None,
            "deep_error": "",
            "deep_reviewed": False,
            "result_recorded": gate_rec is not None,
        }

        # --- Gate outcome check (Python decides, not the LLM) ---
        if not gate_spawn.get("ok", False):
            rec["skipped_reason"] = "gate_spawn_failed"
            progress(change_id, "failed", {"error": gate_spawn.get("error", "gate failed")})
            return rec
        if verdict not in ("PASS", "CONCERNS", "BLOCK"):
            rec["skipped_reason"] = "no_gate_verdict"  # gate left no usable verdict
            progress(change_id, "failed", {"error": "gate produced no verdict"})
            return rec
        # A BLOCK verdict NO LONGER skips Phase 2. A genuine design defect informs
        # the ship decision (and still posts a design comment), but the author wants
        # the whole code review in one pass — so every usable verdict runs Phase 2.
        rec["design_block"] = (verdict == "BLOCK")

        # --- Stage 2: Phase-2 deep review with a bounded convergence loop ---
        # Re-review — feeding prior findings forward — until a round adds no new
        # findings or we hit the configured cap. This maximizes first-pass recall
        # so issues don't drip out across later revisions (the author's pain point).
        progress(change_id, "deep", {"verdict": verdict})   # worker is running Phase 2
        max_rounds = _max_review_rounds()
        deep_spawn = dispatch(build_deep_review_task(link), timeout)
        deep_rec = results.read_result(change_id, root)
        rec["phase2_ran"] = True
        rec["deep_spawn_ok"] = deep_spawn.get("ok", False)
        rec["deep_error"] = deep_spawn.get("error", "")
        rec["deep_reviewed"] = bool((deep_rec or {}).get("deep_reviewed"))
        rec["result_recorded"] = deep_rec is not None
        rounds = 1
        if deep_spawn.get("ok", False):
            # Confirmatory follow-up rounds: each appends only NET-NEW findings.
            # Stop as soon as a round grows the finding set by nothing (converged),
            # or when the round cap is reached.
            prev_count = len((deep_rec or {}).get("findings") or [])
            while rounds < max_rounds:
                followup = dispatch(build_deep_followup_task(link), timeout)
                if not followup.get("ok", False):
                    break   # a failed follow-up never discards the findings we have
                rounds += 1
                deep_rec = results.read_result(change_id, root)
                cur_count = len((deep_rec or {}).get("findings") or [])
                if cur_count <= prev_count:
                    break   # converged: this round surfaced nothing new
                prev_count = cur_count
        rec["deep_rounds"] = rounds
        if not deep_spawn.get("ok", False):
            progress(change_id, "failed", {"error": deep_spawn.get("error", "deep review failed")})
        else:
            counts = (deep_rec or {}).get("counts") or {}
            red, yellow = counts.get("red", 0), counts.get("yellow", 0)
            # Deep review only RECORDS findings. The driver now builds the
            # Python-redacted comment bodies (the surviving 🔴/🟡 findings plus the
            # always-on ship-readiness comment) and a verbatim poster publishes them
            # — so no LLM free-text reaches the CR (security-controls).
            post = _post_pending(change_id, link)
            posted = post["posted_comments"]
            expected = red + yellow + 1   # inline findings + the always-on ship-readiness comment
            rec["posted_comments"] = posted
            rec["posting_expected"] = expected
            rec["post_ok"] = post["post_ok"]
            rec["design_comment_posted"] = post["design_comment_posted"]
            progress(change_id, "done", {
                "counts": {"red": red, "yellow": yellow},
                "design_block": rec.get("design_block", False),
                "posted": posted, "expected": expected,
            })
        return rec

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        per_change = list(pool.map(_one, changes))

    design_blocked = [r for r in per_change if r.get("design_block")]
    failures = [r for r in per_change
                if not r["gate_spawn_ok"] or r.get("deep_spawn_ok") is False]
    result_records = sum(1 for r in per_change if r["result_recorded"])
    summary = {
        "ok": True,
        "changes": len(per_change),
        "gate_spawns": len(per_change),                       # every change is gated
        "deep_spawns": sum(1 for r in per_change if r["phase2_ran"]),
        "design_blocked": len(design_blocked),                # BLOCK verdicts (still deep-reviewed)
        "phase2_skipped_on_block": 0,                         # BLOCK no longer skips Phase 2
        "deep_reviewed": sum(1 for r in per_change if r["deep_reviewed"]),
        "deep_rounds": sum(r.get("deep_rounds", 0) for r in per_change),  # total Phase-2 rounds
        "design_comments_posted": sum(1 for r in per_change if r.get("design_comment_posted")),
        "result_records": result_records,
        "failures": failures,
        "per_change": per_change,
    }
    if generate_report and result_records > 0:
        # Runs AFTER all tasks complete (each dispatch call blocks until its
        # worker session ends its turn and the record is on disk), so the report
        # reflects this run's records. Then archive it as a NEW artifact (one
        # report per run) and, only if that archive succeeds, delete the now-
        # redundant result records — their content lives in the archived report
        # summary and as draft CR comments. Guarded on result_records > 0 so a
        # fully-failed run can't clobber the last good report. Never fails the run.
        try:
            rep = report.generate(root)
            summary["report"] = rep["index"]
            slug = archiver(rep.get("html", ""), root)
            if slug:
                report.set_report_slug(slug, root)
                summary["report_slug"] = slug
                summary["results_cleaned"] = results.clear_results(root)
            else:
                summary["archive_error"] = "report not archived; result records kept"
        except Exception as e:  # pragma: no cover - defensive
            summary["report_error"] = str(e)
    return summary


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage review driver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="Review each change on the reusable worker pool")
    rp.add_argument("--changes", required=True, help="newline/comma-separated links or CR ids")
    rp.add_argument("--concurrency", type=int, default=0,
                    help="parallel reviews; 0 = auto (worker pool concurrency cap)")
    rp.add_argument("--timeout", type=int, default=DEFAULT_TASK_TIMEOUT)
    rp.add_argument("--no-report", dest="report", action="store_false")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        changes = pipeline.parse_batch(args.changes)
        # Standalone CLI: stand up a private worker pool on a background event
        # loop and bridge the (synchronous) driver to it, mirroring how the app
        # backend wires the shared pool. No /api/spawn, no sub-agents.
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        pool = review_pool.ReviewPool()
        dispatch = review_pool.make_sync_dispatch(loop, pool, default_timeout=args.timeout)
        try:
            out = run_review(changes, dispatch=dispatch, concurrency=args.concurrency,
                             timeout=args.timeout, generate_report=args.report)
        finally:
            try:
                asyncio.run_coroutine_threadsafe(pool.shutdown(), loop).result(timeout=30)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
