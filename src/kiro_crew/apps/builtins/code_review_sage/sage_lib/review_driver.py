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


DEFAULT_TASK_TIMEOUT = 5400      # 90 min per review turn (the governing cap — passed
#   through run_review -> _one -> dispatch -> pool.send -> handle.prompt). One heavier
#   single-pass turn replaces up to 5 old turns; the old 30-min cap force-killed
#   working large-PR reviews. Stays under the runtime's 2h prompt default.
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
    gateway sub-agent cap no longer applies — ``review_pool.effective_max_concurrent()``
    is the single source of truth for how many reviews run at once. The pool also
    hard-caps concurrency itself, so this only governs how many tasks the driver offers it."""
    if explicit and explicit > 0:
        return max(1, int(explicit))
    return max(1, review_pool.effective_max_concurrent())


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


def reviewed_key_for(link: str) -> str:
    """Collision-free key for the durable reviewed-index (``reviewed.json``).

    Separate from ``change_id_for``: the change-id is also an on-disk filename and
    is therefore lossily sanitized (``-`` -> ``_``), which let two different repos
    (``acme/service-api`` vs ``acme/service_api``) with the same PR number collide
    on one dedup key and skip a requested review. The reviewed-index key never
    names a file, so it uses the lossless canonical identity instead. Falls back to
    the sanitized change-id for a non-PR link (defensive; repo-review only ever
    feeds real PR URLs from ``list_open_prs``)."""
    try:
        owner, repo, number = pipeline.adapters.github_pr_parts(link)
        return pipeline.adapters.github_review_key(owner, repo, number)
    except pipeline.adapters.AdapterParseError:
        return results.safe_change_id(link)


def _fetch_instruction(link: str) -> str:
    """Platform-aware FETCH instruction for the gate/deep prompts (GitHub only)."""
    try:
        platform = pipeline.adapters.detect_platform(link)
    except Exception:  # pragma: no cover - defensive (empty/odd link)
        platform = "github"
    return pipeline.fetch_spec(platform)


def build_review_task(change_link: str) -> str:
    """Single-pass review prompt: ONE isolated session does the WHOLE review —
    design reasoning AND every code-level dimension — in a single turn, and writes
    the complete result record (phase1 design fields + findings + counts +
    ship_summary + a coverage signal). Design is one dimension of the review, not a
    separate gated stage; the driver runs neither a gate turn nor a convergence
    loop. The session RECORDS findings only — it never posts (the driver builds the
    Python-redacted bodies and a separate poster publishes them verbatim)."""
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "Do the COMPLETE review of EXACTLY ONE change in a SINGLE thorough pass: "
        + change_link + ". There is NO separate gate and NO follow-up round — cover "
        "everything now, carefully, at maximum thinking effort.\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns from active namespaces "
        "(`python3 sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any) and apply it as additional rules.\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `python3 sage_lib/pipeline.py prepare --link " + change_link
        + " --payload-file <file>`.\n"
        "  4. DESIGN dimension (THINK DEEPLY — highest leverage): work the change "
        "through the skill's `Deep design reasoning` lenses (architectural fit, "
        "contract/data evolution, alternatives & proportionality, failure modes, "
        "root-cause vs symptom) as consequence chains; the weakest applicable lens "
        "sets design_risk. Produce gate_verdict (PASS|CONCERNS|BLOCK — BLOCK is ONLY "
        "for a genuine DESIGN defect: no real problem, wrong/over-engineered fix, or a "
        "clearly better alternative ignored; a large blast radius / high criticality "
        "is NEVER on its own a BLOCK), design_risk, criticality, and — ONLY on "
        "CONCERNS/BLOCK — a straightforward, direct design_headline (issue + "
        "recommended direction, no hedging; empty on PASS), plus problem (one "
        "sentence), why_it_matters (one or two SHORT lines), and solution_assessment "
        "(a few 'Label: text' facets on SEPARATE LINES).\n"
        "  5. CODE dimensions: walk EVERY changed hunk against ALL 9 code-level "
        "dimensions + self-critique (Filter/Merge/Sharpen/Stabilize) -> surviving "
        "🔴/🟡 findings. Severity three-tier: 🔴 must-fix (breaks now OR a latent "
        "high-probability/high-impact 'have-to-fix' — do NOT downgrade to 🟡 just "
        "because it works today); 🟡 should-fix; drop nice-to-haves. Keep first-class: "
        "STRICT bidirectional description<->diff fidelity (no phantom claims, no "
        "undocumented change) and an explicit threat chain on every security finding "
        "(entry point -> trust boundary -> exploit -> impact). A design CONCERNS/BLOCK "
        "is ALSO expressed as a finding so it reaches the author.\n"
        "  6. COVERAGE self-check (the driver relies on this): before emitting, "
        "enumerate every changed FILE and confirm you reviewed each against all "
        "dimensions. Set `files_covered` to the list of changed file paths you "
        "actually reviewed, and `coverage_complete` to true ONLY if that list covers "
        "every changed file — otherwise set it false (the driver will run ONE "
        "targeted follow-up on the remainder). Do not pad the list; report honestly.\n"
        "  7. RECORD ONLY — do NOT post any comments. Write data/results/<id>.json: "
        "phase1 (gate_verdict, design_risk, criticality, design_headline, problem, "
        "why_it_matters, solution_assessment) + blast_radius; `findings` (each with "
        "file, line, severity 🔴/🟡, dimension, observation, consequence, suggestion, "
        "snippet, lang); `counts` {red,yellow}; `ship_summary` (ONE straightforward "
        "line: good-to-ship + reason when there are no 🔴, or not-ready + the "
        "must-fix/design reason otherwise); `files_covered`; `coverage_complete`; "
        "deep_reviewed=true. The driver builds the redacted bodies and a separate "
        "poster publishes them — you MUST NOT call any comment tool.\n"
        "  8. If this change is itself a FIX (is_fix), run INLINE miss-analysis "
        "(learn-from-sage): trace the introducing change, ask which dimension was "
        "blind, and STAGE the learning "
        "(`python3 sage_lib/learning.py stage --file <pattern.json> --source fix_introduce`) "
        "— NOT applied to the live ruleset until a human consolidates.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )


def build_review_followup_task(change_link: str) -> str:
    """Bounded coverage backstop — dispatched AT MOST ONCE, and only when the single
    review reported ``coverage_complete=false``. It reviews the STILL-UNCOVERED
    changed files and APPENDS only net-new findings (never repeats/removes existing
    ones), then marks coverage complete. This is NOT the old convergence loop: it
    runs at most one targeted pass, signal-driven, not count-delta-driven."""
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "A prior pass reviewed EXACTLY ONE change: " + change_link + " but reported "
        "INCOMPLETE file coverage (coverage_complete=false) in data/results/<id>.json.\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns "
        "(`python3 sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any) and apply it as additional rules.\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `python3 sage_lib/pipeline.py prepare --link " + change_link
        + " --payload-file <file>`. READ the existing record: its `findings` and "
        "`files_covered`.\n"
        "  4. Review ONLY the changed files NOT already in `files_covered`, against "
        "ALL 9 code dimensions AND the design lenses, with the same three-tier "
        "severity (🔴/🟡, drop nice-to-haves) and the description<->diff fidelity + "
        "security threat-chain checks.\n"
        "  5. RECORD ONLY — APPEND only NET-NEW findings (do NOT repeat, reword, or "
        "remove any already-recorded finding); recompute `counts` {red,yellow} over "
        "the FULL list; refresh `ship_summary`; extend `files_covered` to include "
        "every changed file and set `coverage_complete=true`; keep deep_reviewed=true "
        "and PRESERVE the phase1 block. You MUST NOT call any comment tool.\n"
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

        # --- Single thorough review pass (design is ONE dimension, not a gate) ---
        # No separate gate turn and no convergence loop: ONE dispatch does the whole
        # review (design reasoning + all code dimensions) and writes the complete
        # record. This cuts per-change turns from up to 5 (gate + deep + follow-ups +
        # post) down to review + post, sharply reducing exposure to the per-turn
        # timeout / backend-generation failures that killed the old multi-turn flow.
        progress(change_id, "reviewing", {})
        review_spawn = dispatch(build_review_task(link), timeout)
        rev_rec = results.read_result(change_id, root)
        verdict = str(((rev_rec or {}).get("phase1") or {}).get("gate_verdict", "")).upper()

        # The gate_*/deep_* keys are retained for downstream compatibility — the run
        # summary, _record_reviewed, and the dashboard still read them; with the
        # single-pass model they simply reflect the ONE review dispatch (there is no
        # longer a distinct gate).
        rec: dict = {
            "change": link, "change_id": change_id,
            "gate_spawn_ok": review_spawn.get("ok", False),
            "gate_error": review_spawn.get("error", ""),
            "gate_verdict": verdict or "UNKNOWN",
            "phase2_ran": review_spawn.get("ok", False),
            "deep_spawn_ok": review_spawn.get("ok", False),
            "deep_error": review_spawn.get("error", ""),
            "deep_reviewed": bool((rev_rec or {}).get("deep_reviewed")),
            "result_recorded": rev_rec is not None,
            "design_block": (verdict == "BLOCK"),
            "deep_rounds": 1,
        }

        # Fail only when the turn failed OR nothing usable was recorded — never
        # discard a record that DID land (the old ok-before-recorded ordering
        # discarded already-written verdicts/findings on a trailing abnormal stop).
        if not review_spawn.get("ok", False):
            rec["skipped_reason"] = "review_failed"
            progress(change_id, "failed", {"error": review_spawn.get("error", "review failed")})
            return rec
        if not rec["deep_reviewed"]:
            rec["skipped_reason"] = "no_review_recorded"  # turn completed but wrote no review
            progress(change_id, "failed", {"error": "review produced no result record"})
            return rec

        # --- Bounded coverage backstop: AT MOST ONE targeted follow-up, and only
        # when the review self-reported incomplete file coverage. This replaces the
        # blanket 3-round convergence loop with a single, signal-driven pass; a
        # failed follow-up keeps whatever the first pass recorded.
        if (rev_rec or {}).get("coverage_complete") is False:
            progress(change_id, "reviewing", {"coverage": "followup"})
            followup = dispatch(build_review_followup_task(link), timeout)
            if followup.get("ok", False):
                rev_rec = results.read_result(change_id, root) or rev_rec
                rec["deep_rounds"] = 2
                rec["deep_reviewed"] = bool((rev_rec or {}).get("deep_reviewed"))

        counts = (rev_rec or {}).get("counts") or {}
        red, yellow = counts.get("red", 0), counts.get("yellow", 0)
        # The review only RECORDS findings; the driver builds the Python-redacted
        # comment bodies and a separate poster publishes them verbatim — no LLM
        # free-text reaches the CR (security control, unchanged).
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
