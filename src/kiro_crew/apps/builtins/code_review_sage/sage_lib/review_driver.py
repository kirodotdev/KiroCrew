#!/usr/bin/env python3
"""Review driver — code-enforced two-stage review loop.

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
import inspect
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Optional KiroCrew runtime dep (absent when running standalone / in tests).
# Kept at module top per the imports guideline; guarded at each use site.
try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore

# Every loopback call below carries X-Internal-Secret, and urlopen honours
# HTTP_PROXY for loopback addresses, so a proxied environment would send the
# secret to the proxy in cleartext. The fallback must therefore stay
# proxy-disabled too -- degrading to a bare urlopen would leave the standalone
# path (`python3 sage_lib/review_driver.py`) carrying the leak this closes.
#
# It must ALSO refuse redirects, for the same reason the real helper does. An
# earlier version of this fallback omitted that on the grounds that "these
# endpoints never return a redirect" -- wrong for `_probe`, whose entire job is
# to dial candidate ports that may have something other than our gateway
# listening. A 302 from one of those would replay the secret to whatever host
# Location names.
try:
    from kiro_crew.loopback_http import loopback_urlopen  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback

    class _FallbackNoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def loopback_urlopen(req: urllib.request.Request | str, timeout: float):  # type: ignore
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _FallbackNoRedirect()
        ).open(req, timeout=timeout)

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/review_driver.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import (  # noqa: E402
    adapters,
    discovery,
    pipeline,
    report,
    results,
    review_pool,
    store,
)


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs from LLM-generated text before it is
    posted to an external surface (the dashboard artifact store). No-op when the
    KiroCrew redaction lib isn't importable (standalone)."""
    if redact_exfiltration_urls is None or redact_credentials is None:
        return text
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


DEFAULT_TASK_TIMEOUT = 5400      # 90 min per review turn (the governing cap — passed
#   through run_review -> _one -> dispatch -> pool.send -> handle.prompt). A single
#   thorough pass needs headroom that a 30-min cap would force-kill on large PRs.
#   Stays under the runtime's 2h prompt default.
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
        with loopback_urlopen(req, timeout=timeout) as resp:
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


def archive_report(html_body: str, root: Path | None = None) -> str | None:
    """Publish a report HTML body as a dashboard artifact; returns the slug or None.

    Public entry point for the backend's on-demand "share / export" route, which
    re-archives a run whose automatic archive failed. Same redaction and pruning
    as the automatic path."""
    return _archive_report(html_body, root)


def _resolve_concurrency(explicit: int | None = None) -> int:
    """Effective driver fan-out: an explicit value wins; otherwise default to the
    worker pool's concurrency cap.

    Pool workers are direct ACP sessions (NOT ``/api/spawn`` sub-agents), so the
    gateway sub-agent cap does not apply — ``review_pool.effective_max_concurrent()``
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


def build_consolidation_task(namespace: str, live_path: str, candidate_path: str,
                             out_path: str) -> str:
    """Prompt for the one-shot merge that turns staged candidates into the ruleset.

    The judgment is the model's: whether a candidate is already covered, whether
    two rules should collapse into one, and how to phrase the survivor. The
    mechanics are not — the worker WRITES a file and the caller applies it through
    ``learning.consolidate_apply``, which refuses empty content. That split is why
    a bad merge cannot silently wipe the ruleset.
    """
    return (
        "You are consolidating Code Review Sage's learned-pattern ruleset for the "
        f"namespace {namespace!r}. This is a one-shot merge, not a review.\n"
        f"  * CURRENT ruleset (what reviews load today): {live_path}\n"
        f"  * PENDING candidates (staged, not yet used):  {candidate_path}\n"
        "Read both, then write the merged ruleset to "
        f"{out_path}.\n"
        "Rules for the merge:\n"
        "  1. Keep every current pattern unless a candidate genuinely supersedes "
        "it — this file IS the reviewer's memory, so dropping a rule loses a "
        "lesson permanently. Deletion needs a reason you could defend.\n"
        "  2. Collapse duplicates and near-duplicates into ONE sharper rule. "
        "Several candidates often come from the same incident.\n"
        "  3. Each pattern is a single high-level, code-agnostic heuristic: a "
        "title plus one paragraph of guidance. Strip the incident anecdote, the "
        "repo name, the PR number and any code sample — if a rule needs an "
        "example to be understood it is underspecified, so sharpen the wording "
        "instead.\n"
        "  4. Preserve the exact on-disk format, one block per pattern:\n"
        "     ### <title> <!-- scope:common --> <!-- impact:high|medium|low --> "
        "<!-- added:<ISO8601Z> -->\n"
        "     <one paragraph of guidance on a single line>\n"
        "  5. Keep the file's leading markdown header. Write NOTHING else to the "
        "file — no commentary, no fences.\n"
        "Report in your final message how many patterns you kept, merged and "
        "dropped, and why anything was dropped."
    )


def _accepts_activity(dispatch: Callable[..., Any]) -> bool:
    """Whether a dispatch callable takes the ``on_activity`` reporter.

    Inspected once per change rather than probed with try/except TypeError, which
    would also swallow a TypeError raised from INSIDE the dispatcher and silently
    downgrade to no progress."""
    try:
        return "on_activity" in inspect.signature(dispatch).parameters
    except (TypeError, ValueError):
        return False


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
    ones), then marks coverage complete. It runs at most one targeted pass,
    signal-driven, not count-delta-driven."""
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
        cfg = store.crew_home() / "config.json"
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
    401/404, means it's there; only connection errors mean it isn't).

    Proxy-disabled deliberately: ``_gateway_base`` calls this once per candidate
    port, so a cold resolve that misses sends the secret up to len(candidates)
    times -- this is the highest-multiplicity secret-bearing send in the app."""
    try:
        req = urllib.request.Request(base + "/api/spawn",
                                     headers={"X-Internal-Secret": secret} if secret else {})
        with loopback_urlopen(req, timeout=3) as resp:
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
    try:
        return (store.crew_home() / ".local_secret").read_text(encoding="utf-8").strip()
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


def post_recorded(change_id: str, link: str, *, dispatch, root: Path | None = None,
                  run_id: str | None = None,
                  timeout: float = DEFAULT_TASK_TIMEOUT,
                  keys: list[str] | None = None, confirm=None) -> dict:
    """Publish an ALREADY-RECORDED review to its pull request.

    Builds the draft comment bodies from the recorded findings plus the always-on
    ship-readiness comment, REDACTS each in Python (``pipeline.build_pending_comments``
    -> ``_redact``), persists them into the record, then dispatches the verbatim
    poster. Redaction is deterministic HERE — no LLM free-text reaches the pull
    request, which is the security property the split between reviewer and poster
    exists to guarantee.

    Used by two callers: the opt-in ``review.auto_post`` path inside a run, and
    the explicit "post comments" action, which is the same operation deferred
    until the user asks for it. Returns posting stats; no poster is spawned when
    there is nothing to post.

    ``keys`` selects individual comments (see ``build_pending_comments``) so the
    author can send the findings they agree with and leave the rest. Already-posted
    keys are dropped from the selection: each call creates its own pending review
    on GitHub, so re-sending one would duplicate it on the pull request. Omitting
    ``keys`` posts everything not yet posted.
    """
    cur = results.read_result(change_id, root, run_id)
    if not cur:
        # No record means no review to publish. Without this the always-on
        # ship-readiness comment would be built from an empty record and posted as
        # a review of nothing (and the write-back would fail validation).
        return {"post_ok": True, "posted_comments": 0,
                "design_comment_posted": False, "pending": 0,
                "post_error": "no recorded review for this change"}
    all_entries = pipeline.build_pending_comments(cur)
    already = set(cur.get("posted_keys") or [])
    wanted = (set(keys) if keys is not None
              else {str(e.get("key")) for e in all_entries})
    new = [e for e in all_entries
           if str(e.get("key")) in wanted and str(e.get("key")) not in already]
    if not new:
        return {"post_ok": True, "posted_comments": 0,
                "design_comment_posted": False, "pending": 0,
                "posted_keys": sorted(already),
                "post_error": "nothing left to post" if all_entries else ""}
    # The draft is the UNION of what is already drafted and what was just
    # selected — not the selection alone.
    #
    # GitHub allows one pending review per author, so the poster DELETES the
    # existing sage draft and creates a replacement. A payload holding only the
    # new selection therefore does not add to the draft, it REPLACES it: post
    # finding A, then finding B, and A is deleted with the old draft and never
    # reappears — while `posted_keys` still claims A landed, so nothing would
    # ever re-send it. Rebuilding the full draft each time keeps every comment
    # the author chose.
    #
    # If the author submitted the previous draft on GitHub in between, there is no
    # pending review to replace and the re-included comments post a second time.
    # That is the deliberate trade this module already takes elsewhere: a visible
    # duplicate can be removed, a silently dropped finding cannot be recovered.
    pending = [e for e in all_entries
               if str(e.get("key")) in (wanted | already)]
    cur["pending_comments"] = pending
    # GitHub posts a single PENDING review, so assemble the deterministic,
    # already-redacted envelope in Python here — the poster posts it verbatim via
    # one `gh api` call and never composes bodies.
    try:
        _platform = pipeline.adapters.detect_platform(link)
    except Exception:  # pragma: no cover - defensive
        _platform = "github"
    if _platform == "github":
        # A record with no `revision` cannot be anchored, and the builder refuses
        # rather than let GitHub bind the draft to the current head. Surface that as
        # a post failure on the record: the run reports it, the findings stay on disk
        # for a retry once the record is repaired, and nothing reaches the pull
        # request. Letting it raise would abort the whole batch for one bad record.
        try:
            cur["github_review_payload"] = pipeline.build_github_review_payload(cur)
        except ValueError as e:
            cur["post_ok"] = False
            cur["post_error"] = str(e)
            cur["posted_comments"] = 0
            cur["design_comment_posted"] = False
            results.write_result(cur, root, run_id)
            return {"post_ok": False, "post_error": str(e), "posted_comments": 0,
                    "design_comment_posted": False, "pending": len(pending),
                    "expected_units": 0, "posted_keys": list(already)}
    # Clear the delivery fields before the record goes to the poster. They are
    # what the poster writes back as its ONLY evidence of delivery, so a value
    # left over from an earlier attempt is indistinguishable from one it just
    # wrote: a first post that partially failed leaves `posted_comments` at 3,
    # the one-comment retry publishes that record, a poster that delivers
    # nothing writes nothing, and `3 >= 1` then marks the comment delivered and
    # adds it to `posted_keys` — permanently skipping a finding that was never
    # posted. Zeroing them means the check can only ever pass on a count written
    # by THIS attempt. `posted_keys` is deliberately not cleared: it is the
    # durable ledger of what really landed, and forgetting it would duplicate.
    # The posting-skipped path below already resets these two for the same
    # reason; this is the sibling that did not.
    cur["posted_comments"] = 0
    cur["design_comment_posted"] = False
    results.write_result(cur, root, run_id)
    # The poster reads github_review_payload from the shared path named in its
    # prompt, and writes posted_comments back there.
    #
    # A False return on a RUN-SCOPED record means the trusted record is NOT what
    # sits at that path -- `publish_to_shared` refuses when its own no-follow read
    # is blocked, which is exactly the case where a sibling worker replaced the
    # record with a link. Dispatching anyway would point the poster at whatever IS
    # there and publish it to the pull request, so the failure has to abort.
    #
    # Without a run_id the record already IS the shared one: publishing is a no-op
    # that also reports False, and treating that as refusal would abort every
    # unscoped post. The guard therefore applies only where a copy was required.
    if run_id and not results.publish_to_shared(change_id, root, run_id):
        staged = "could not stage the review record for the poster"
        cur["post_ok"] = False
        cur["post_error"] = staged
        results.write_result(cur, root, run_id)
        return {"post_ok": False, "post_error": staged, "posted_comments": 0,
                "design_comment_posted": False, "pending": len(pending),
                "expected_units": 0, "posted_keys": list(already)}
    spawn = dispatch(build_post_task(link), timeout)
    results.adopt_from_shared(change_id, root, run_id)
    after = results.read_result(change_id, root, run_id) or {}
    ok = bool(spawn.get("ok", False))
    # The poster writes the count it actually delivered. That write is the ONLY
    # evidence of delivery — a spawn that merely returned cleanly proves nothing,
    # and treating it as proof would break the guard that catches a poster which
    # posted nothing (_record_reviewed refuses to mark a PR reviewed unless
    # posted >= expected). ``posted_comments`` is therefore never overwritten.
    delivered = int(after.get("posted_comments", 0) or 0)
    # Compare against PAYLOAD UNITS, not the finding count. The poster reports
    # `len(comments) + 1 if body`, and a finding without a usable anchor folds into
    # the body instead of becoming its own inline comment — so `len(pending)`
    # over-counts and a complete delivery read as short. `posted_keys` then went
    # unwritten and the next post duplicated comments already on the pull request.
    # Non-GitHub platforms have no payload; there the finding count is the unit count.
    expected_units = (pipeline.review_payload_units(cur["github_review_payload"])
                      if _platform == "github" else len(pending))
    # `confirm` is a seam, not a bypass: it defaults to the real read-back and
    # exists so tests about WHICH comments a rebuilt draft carries do not each
    # need a live pull request.
    _confirm = confirm or _draft_confirmed
    # One confirmation, two consumers. `posted_keys` is the durable per-finding
    # ledger; `post_ok` is what `_record_reviewed` reads to index the pull request as
    # reviewed and what `_all_delivered` reads before CLEARING the result records.
    # Gating only the ledger left the other two riding on the poster's own report, so
    # a fabricated count still marked the PR reviewed and deleted the records the
    # retry would have needed -- the more damaging half of the same hole.
    #
    # The PAYLOAD is what gets confirmed, not its size: a count is satisfied by any
    # draft of the right shape, including a previous run's draft the poster never
    # replaced.
    confirmed_id = str(_confirm(link, cur.get("github_review_payload") or {}) or "")
    confirmed = bool(ok) and bool(confirmed_id)
    if confirmed:
        # Record WHICH comments landed, not just how many: the count cannot tell a
        # later call what is already on the pull request, and that is what stops a
        # second post from duplicating it.
        #
        # The gate is a read-back from GitHub, NOT the poster's own report. The
        # poster is an LLM session and `posted_comments` is a number it writes about
        # itself (see `build_post_task` step 4), so a prompt-injected reviewer could
        # claim a delivery that never happened. Fail-closed: an unverifiable delivery
        # leaves the ledger untouched and reports failure, so the records survive and
        # the next post re-sends. A visible duplicate can be removed; a silently
        # dropped finding cannot be recovered.
        after["posted_keys"] = sorted(
            already | {str(e.get("key")) for e in pending})
        # A confirmed delivery makes the poster's self-reported count redundant, so
        # the read-back's own accounting replaces it. Leaving the poster's number in
        # place let a correct delivery be under-reported: the draft is proven on the
        # pull request, but `_record_reviewed` compares posted against expected and
        # refuses to index the head, so the next run posts the same review again.
        after["posted_comments"] = expected_units
        # WHICH draft was delivered, so a view can tell "this run posted at some
        # point" from "the draft pending right now is this run's". A later run
        # replaces the draft by deleting and re-creating it, so a changed id is the
        # signal that the pending draft belongs to someone else.
        after["posted_review_id"] = confirmed_id
        results.write_result(after, root, run_id)
    elif ok and delivered:
        # A partial post cannot be attributed to specific comments, so nothing is
        # marked delivered. Re-posting the selection is the safe direction: a
        # duplicate is visible and removable, a silently-dropped finding is not.
        after["post_partial"] = True
    return {
        # `post_ok` means DELIVERED, not "the spawn exited cleanly". Two readers
        # depend on that meaning: `_record_reviewed` indexes the pull request as
        # reviewed, and `_all_delivered` clears the result records afterwards. A
        # spawn that returned cleanly having posted nothing must not satisfy either.
        "post_ok": confirmed,
        "post_error": (
            spawn.get("error", "")
            or ("" if confirmed else
                "the posted draft could not be confirmed on the pull request")),
        # Authoritative once confirmed: `after["posted_comments"]` was replaced with
        # the payload's own unit count above, so this no longer echoes the poster.
        "posted_comments": int(after.get("posted_comments", 0) or 0),
        "design_comment_posted": bool(after.get("design_comment_posted")),
        "pending": len(pending),
        # The number of deliverable units actually sent, so the caller can set
        # `posting_expected` from what was sent rather than recomputing it from
        # finding counts (which miscounts folded-in unanchored findings).
        "expected_units": expected_units,
        "posted_keys": list(after.get("posted_keys") or []),
        # Empty unless this attempt confirmed a draft, so a caller can never mistake
        # an earlier run's id for the one pending now.
        "posted_review_id": str(after.get("posted_review_id") or ""),
    }


def _confirm_text(value: object) -> str:
    """Normalize a body for comparison: newline form, and trailing space per line.

    GitHub echoes a review body back verbatim except for line-ending form, so this
    is the smallest normalization that keeps the comparison an IDENTITY check rather
    than a fuzzy match. Anything beyond CRLF and trailing blanks is a real
    difference and must fail the comparison.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _draft_confirmed(link: str, payload: dict) -> str:
    """Return the id of the sage draft carrying exactly `payload`, or "" if unproven.

    The id, not a boolean, because "which draft did we confirm" is the fact callers
    need: the poster replaces a stale draft by DELETING and re-creating it, so a later
    run's draft always has a different id. Recording the id is what lets a view prove
    the draft pending right now is the one ITS run delivered, rather than one a
    subsequent run put there.

    Delivery evidence must not come from the process that claims to have delivered.
    The poster is an LLM session that writes its own `posted_comments`, so this reads
    the pull request's PENDING reviews back through the app's own `gh api` chokepoint
    and compares what is there against the payload that was sent.

    Comparing on CONTENT rather than a unit count is what makes the read-back mean
    anything. A count alone is satisfied by any draft of the right size, including a
    previous run's draft that the poster never replaced: the obsolete findings would
    be marked delivered, the pull request indexed as reviewed, the retryable records
    cleared, and the stale draft is what the publish button then offers. So all three
    identifying parts must match -- the body, the anchoring commit, and every inline
    comment with its own anchor -- because a review is only the same review if it
    says the same things about the same lines of the same revision.

    All-or-nothing is faithful here rather than a simplification: the draft is
    created by ONE POST carrying every inline comment, so a partially-delivered
    review is not a state GitHub can be left in -- the call either creates the whole
    thing or fails.

    Returns "" on any doubt -- no draft, different content, a non-GitHub platform,
    a `gh` failure, a timeout. "" means "not proven delivered", which leaves the
    durable ledger untouched and lets the next post re-send.
    """
    if pipeline.review_payload_units(payload) <= 0:
        return ""        # nothing was sent -> nothing to confirm
    # An unanchored draft is not identifiable, and the payload builder already
    # refuses to produce one; requiring it here means a draft can never be
    # confirmed against a revision the record does not name.
    expected_commit = str(payload.get("commit_id") or "")
    if not expected_commit:
        return ""
    try:
        owner, repo, number = adapters.github_pr_parts(link)
    except Exception:
        return ""        # not a GitHub pull request URL -> nothing to confirm
    try:
        reviews = discovery.run_gh_json(
            # `jq` is required with `paginate`, not decoration: `gh --paginate`
            # concatenates one JSON array per page, and the reader only whole-parses
            # when no jq is given, so page two onward makes the document invalid.
            # `.[]` streams the elements as JSONL instead. A parse failure here reads
            # as "unproven", so a busy pull request would silently never confirm.
            f"repos/{owner}/{repo}/pulls/{number}/reviews", jq=".[]", paginate=True)
    except Exception:
        return ""        # gh unavailable / not authorized / timeout -> unproven
    for rev in reviews:
        if str(rev.get("state") or "") != "PENDING":
            continue
        if pipeline.DRAFT_MARKER not in str(rev.get("body") or ""):
            continue        # a human's in-progress draft, not ours
        rid = rev.get("id")
        if rid is None:
            continue
        if _confirm_text(rev.get("body")) != _confirm_text(payload.get("body")):
            return ""    # some other sage draft, not the one just sent
        if str(rev.get("commit_id") or "") != expected_commit:
            return ""    # right text, wrong revision -> anchored to other code
        try:
            comments = discovery.run_gh_json(
                f"repos/{owner}/{repo}/pulls/{number}/reviews/{rid}/comments",
                jq=".[]", paginate=True)
        except Exception:
            return ""
        want = sorted(
            (str(c.get("path") or ""), int(c.get("line") or 0),
             _confirm_text(c.get("body")))
            for c in (payload.get("comments") or []))
        # `line` reads null on a comment GitHub considers outdated, where the
        # position survives as `original_line`. Accepting that fallback avoids a
        # false negative without loosening identity: path, body and the review's
        # commit still have to match.
        got = sorted(
            (str(c.get("path") or ""),
             int(c.get("line") or c.get("original_line") or 0),
             _confirm_text(c.get("body")))
            for c in comments)
        return str(rid) if want == got else ""
    return ""


def run_review(changes: list[str], *, dispatch=None, archiver=_default_archiver,
               concurrency: int = 0, timeout: int = DEFAULT_TASK_TIMEOUT,
               generate_report: bool = True, root: Path | None = None,
               progress=None, run_id: str | None = None, cancelled=None,
               post: bool | None = None, confirm=None) -> dict:
    """Two-stage per change (bounded concurrency): a Phase-1 gate task, then a
    Phase-2 deep-review task for every usable verdict (PASS / CONCERNS / BLOCK).
    Each task is dispatched to the reusable worker pool (``dispatch``) and the
    call returns when that task's session finishes its turn. The driver reads
    the gate verdict; a BLOCK no longer skips Phase 2 (it only informs the ship
    decision), then builds the Focus Report. Returns a deterministic summary.

    ``dispatch`` is an injected ``(task, timeout) -> {ok, output, error}`` callable
    (the app backend wires ``review_pool.make_sync_dispatch``; tests inject a fake).
    ``concurrency`` <= 0 means auto: default to the worker pool's concurrency
    cap (``review_pool.MAX_CONCURRENT``).

    ``run_id`` scopes this run's result records and report to
    ``data/runs/<run_id>/`` instead of the shared dirs, which is what makes it safe
    for several runs to be in flight at once. Omitting it keeps the legacy shared
    layout (standalone CLI use).

    ``cancelled`` is an optional zero-arg predicate polled between changes. When it
    turns true, changes that have not started are skipped and reported as
    ``cancelled``. A change already mid-dispatch runs to completion — the worker
    session owns an in-flight model turn that cannot be torn down mid-stream
    without corrupting the pool — so cancellation is prompt but not instant.

    ``post`` overrides the ``review.auto_post`` config for this run: findings are
    published back to the pull request as a PENDING (draft) review only when it is
    enabled. It defaults to OFF, because the review is meant to be READ in the
    app, and writing to a pull request is a side effect the user asks for rather
    than a consequence of running a review."""
    if run_id:
        store.ensure_run_layout(run_id, root)
    store.ensure_layout(root)
    changes = [c for c in changes if c]
    if not changes:
        return {"ok": False, "error": "no changes to review", "spawned": 0}
    dispatch = dispatch or _unconfigured_dispatch
    progress = progress or (lambda *a, **k: None)   # (change_id, phase, extra) sink
    is_cancelled = cancelled or (lambda: False)

    # Whether to publish findings back to the pull request. Read ONCE per run so a
    # mid-run config edit cannot post some PRs and not others. Explicit `is True`
    # so a stray string ("false") can never enable writing to a PR.
    if post is None:
        try:
            _review_cfg = store.load_config(root).get("review") or {}
        except Exception:  # pragma: no cover - defensive
            _review_cfg = {}
        auto_post = _review_cfg.get("auto_post") is True
    else:
        auto_post = bool(post)

    # Clean slate for this run: clear the previous run's displayed report and any
    # leftover result records, so a new review never shows confusing prior-run
    # data. The previous report is already archived as an artifact (history kept).
    # For a run-scoped run the dir is fresh anyway; this keeps the legacy path
    # behaving exactly as before.
    report.reset(root, run_id)
    results.clear_results(root, run_id)
    # Also sweep the SHARED staging path for the changes this run will review.
    #
    # The worker writes its record to the shared dir and the driver adopts it into
    # the run dir; a crash between those two steps leaves an orphan that nothing
    # reaps (`_reap_orphan_run_dirs` only walks `data/runs/`). Without this sweep,
    # the next review of that change whose worker completes but records nothing
    # would adopt the residue and report a stale review as a fresh success — and
    # `_record_reviewed` would then durably mark the change reviewed at the NEW
    # head. The legacy whole-run flow got this for free by clearing the whole
    # shared dir at run start; a run-scoped run has to clear its own keys.
    results.clear_staged([_cid(c) for c in changes], root)

    # Mark everything queued upfront so the page renders all rows at once.
    for _link in changes:
        progress(_cid(_link), "queued", {})

    concurrency = _resolve_concurrency(concurrency)
    per_change: list[dict] = []

    def _post_pending(change_id: str, link: str) -> dict:
        return post_recorded(change_id, link, dispatch=dispatch, root=root,
                             run_id=run_id, timeout=timeout, confirm=confirm)

    def _one(link: str) -> dict:
        change_id = _cid(link)

        # Cooperative cancellation checkpoint. A change that has not started yet is
        # dropped here rather than paying for a full review the user already
        # abandoned; one already past this point runs to completion (see the
        # docstring — an in-flight worker turn cannot be torn down safely).
        if is_cancelled():
            progress(change_id, "cancelled", {})
            return {
                "change": link, "change_id": change_id,
                "gate_spawn_ok": False, "gate_error": "", "gate_verdict": "CANCELLED",
                "phase2_ran": False, "deep_spawn_ok": False, "deep_error": "",
                "deep_reviewed": False, "result_recorded": False,
                "design_block": False, "deep_rounds": 0, "cancelled": True,
                "skipped_reason": "cancelled",
            }

        # --- Single thorough review pass (design is ONE dimension, not a gate) ---
        # No separate gate turn and no convergence loop: ONE dispatch does the whole
        # review (design reasoning + all code dimensions) and writes the complete
        # record. Keeping it to review + post (rather than gate + deep + follow-ups +
        # post) minimizes exposure to per-turn timeout / backend-generation failures.
        progress(change_id, "reviewing", {})
        # A single-PR review is ONE long worker turn, so "reviewing" alone leaves
        # the UI with nothing to show for minutes. The pool reports each tool the
        # reviewer invokes; relay it as live activity on this change's phase.
        # Only the real pool dispatch accepts the reporter — test fakes and the
        # standalone CLI pass a plain (task, timeout) callable, so this is opt-in
        # by signature rather than by a TypeError retry that could mask a genuine
        # argument bug inside the dispatcher.

        def report(tool: str, step: int) -> None:
            # Guarded here as well as in the pool: activity is decoration, and a
            # progress writer that raises must never be able to fail a review
            # that is otherwise going fine.
            try:
                progress(change_id, "reviewing",
                         {"activity": {"tool": tool, "step": step}})
            except Exception:
                pass

        # Nothing may be sitting at this change's shared path when its reviewer starts.
        # Adoption proves only that a record NAMES this change, not who wrote it, and every
        # worker can write any change's path in the shared dir -- so a record present
        # beforehand is a leftover or another worker's plant, and adopting it would put
        # someone else's findings on this pull request. If the slot cannot be cleared, skip
        # adoption rather than trust it.
        slot_clear = results.stake_shared(change_id, root)
        if _accepts_activity(dispatch):
            review_spawn = dispatch(build_review_task(link), timeout,
                                    on_activity=report)
        else:
            review_spawn = dispatch(build_review_task(link), timeout)
        # The worker writes the shared data/results/<id>.json its prompt names;
        # move it into this run's private dir before reading. Without this the
        # run's dir stays empty and a completed review reports no findings.
        if slot_clear:
            results.adopt_from_shared(change_id, root, run_id)
        rev_rec = results.read_result(change_id, root, run_id)
        verdict = str(((rev_rec or {}).get("phase1") or {}).get("gate_verdict", "")).upper()

        # The gate_*/deep_* keys are kept for downstream compatibility — the run
        # summary, _record_reviewed, and the dashboard read them; with the
        # single-pass model they reflect the ONE review dispatch (there is no
        # distinct gate).
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
        # discard a record that DID land, so a trailing abnormal stop cannot drop
        # already-written verdicts/findings.
        if not review_spawn.get("ok", False):
            rec["skipped_reason"] = "review_failed"
            progress(change_id, "failed", {"error": review_spawn.get("error", "review failed")})
            return rec
        if not rec["deep_reviewed"]:
            rec["skipped_reason"] = "no_review_recorded"  # turn completed but wrote no review
            progress(change_id, "failed", {"error": "review produced no result record"})
            return rec

        # --- Bounded coverage backstop: AT MOST ONE targeted follow-up, and only
        # when the review self-reported incomplete file coverage — a single,
        # signal-driven pass; a failed follow-up keeps whatever the first pass
        # recorded.
        if (rev_rec or {}).get("coverage_complete") is False:
            progress(change_id, "reviewing", {"coverage": "followup"})
            # The follow-up turn UPDATES the record, so it needs the current one
            # visible at the path its prompt names, and re-adopted afterwards. A
            # failed publish means the record there is not ours, and the follow-up
            # would adopt whatever replaced it -- skip the turn instead.
            published = results.publish_to_shared(change_id, root, run_id)
            followup = (dispatch(build_review_followup_task(link), timeout)
                        if published or not run_id else {"ok": False})
            if followup.get("ok", False):
                results.adopt_from_shared(change_id, root, run_id)
                rev_rec = results.read_result(change_id, root, run_id) or rev_rec
                rec["deep_rounds"] = 2
                rec["deep_reviewed"] = bool((rev_rec or {}).get("deep_reviewed"))

        counts = (rev_rec or {}).get("counts") or {}
        red, yellow = counts.get("red", 0), counts.get("yellow", 0)
        if not auto_post:
            # Default path: the review is READ in the app. Nothing is written to
            # the pull request — publishing to someone else's PR is a side effect
            # the user opts into (``review.auto_post``), not a consequence of
            # looking at a review.
            #
            # ``posting_expected`` is 0 rather than red+yellow+1 so the durable
            # dedup index still accepts this change: _record_reviewed requires
            # posted >= expected, which is how it refuses to mark a PR reviewed
            # when a post half-failed. With posting off there is nothing to
            # deliver, so 0 >= 0 correctly means "this PR was reviewed".
            rec["posting_skipped"] = True
            rec["posted_comments"] = 0
            rec["posting_expected"] = 0
            rec["post_ok"] = True
            rec["design_comment_posted"] = False
            progress(change_id, "done", {
                "counts": {"red": red, "yellow": yellow},
                "design_block": rec.get("design_block", False),
                "posted": 0, "expected": 0,
            })
            return rec
        # Opt-in path: the review only RECORDS findings; the driver builds the
        # Python-redacted comment bodies and a separate poster publishes them
        # verbatim — no LLM free-text reaches the CR (security control, unchanged).
        post = _post_pending(change_id, link)
        posted = post["posted_comments"]
        # What the poster was actually asked to deliver, not red+yellow+1: an
        # unanchored finding folds into the review body rather than becoming its
        # own inline comment, so the finding count over-states the payload. This
        # number gates `_record_reviewed` and `_all_delivered`, so over-stating it
        # left a fully-delivered review looking short in both.
        expected = int(post.get("expected_units") or 0)
        # Shared with the explicit-retry path in the backend, so a retry records
        # delivery exactly the way the first attempt would have.
        apply_post_outcome(rec, post)
        progress(change_id, "done", {
            "counts": {"red": red, "yellow": yellow},
            "design_block": rec.get("design_block", False),
            "posted": posted, "expected": expected,
        })
        return rec

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        per_change = list(pool.map(_one, changes))

    design_blocked = [r for r in per_change if r.get("design_block")]
    cancelled_changes = [r for r in per_change if r.get("cancelled")]
    failures = [r for r in per_change
                if not r.get("cancelled")
                and (not r["gate_spawn_ok"] or r.get("deep_spawn_ok") is False)]
    result_records = sum(1 for r in per_change if r["result_recorded"])
    summary = {
        "ok": True,
        "changes": len(per_change),
        "gate_spawns": len(per_change),                       # every change is gated
        "deep_spawns": sum(1 for r in per_change if r["phase2_ran"]),
        "design_blocked": len(design_blocked),                # BLOCK verdicts (still deep-reviewed)
        "phase2_skipped_on_block": 0,                         # BLOCK does not skip Phase 2
        "cancelled": len(cancelled_changes),
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
        #
        # The report is written to the run's own dir FIRST and kept there
        # regardless of whether the artifact archive succeeds — the in-app report
        # view reads that file, so a failed archive no longer means "no report".
        try:
            rep = report.generate(root, run_id=run_id)
            summary["report"] = rep["index"]
            slug = archiver(rep.get("html", ""), root)
            if slug:
                report.set_report_slug(slug, root, run_id)
                summary["report_slug"] = slug
                # Records are only redundant once their content has actually been
                # DELIVERED. With posting deferred to an explicit action they are
                # the only source of the redacted comment payload, so clearing
                # them here would silently make "post comments" impossible.
                #
                # `auto_post` alone is NOT delivery evidence — it is the intent to
                # post. A run whose posts half-failed (network error, permission
                # loss, a partial batch) still reaches here, and deleting the
                # records then leaves the explicit posting retry with nothing to
                # send while the PR carries only some of the findings. Require the
                # same condition the durable dedup index enforces: every change
                # reported post_ok AND delivered at least what it expected.
                if auto_post and _all_delivered(per_change):
                    summary["results_cleaned"] = results.clear_results(root, run_id)
                elif auto_post:
                    summary["results_kept_undelivered"] = True
            else:
                summary["archive_error"] = "report not archived; result records kept"
        except Exception as e:  # pragma: no cover - defensive
            summary["report_error"] = str(e)
    return summary


def apply_post_outcome(rec: dict, post: dict) -> None:
    """Write a post result's delivery evidence onto a per-change record.

    The durable dedup index (``_record_reviewed``) and the run verdict
    (``_all_delivered``) both decide "was this actually delivered?" by reading
    exactly these four fields off the per-change record. Every path that delivers
    comments must therefore write them the SAME way, or the two readers disagree
    with reality: an explicit retry that succeeded but left the record showing the
    original failure keeps the PR out of the index, and the next repo review
    re-reviews and re-posts it.

    ``posting_expected`` comes from the payload units the poster was actually asked
    to deliver (``expected_units``), never from a finding count -- an unanchored
    finding folds into the review body instead of becoming its own inline comment.

    ``posted_keys`` names WHICH findings landed, which is what the UI needs rather
    than a count: it marks individual findings as sent, and it is the evidence the
    publish action requires before releasing the pending review. Omitting it here
    left the auto-post path delivering a draft the app then refused to publish,
    because the run carried no record of which change the draft belonged to.

    ``posted_review_id`` names WHICH DRAFT carries them. A later run replaces the
    draft by deleting and re-creating it, so without the id an earlier run's view
    cannot tell its own draft from that replacement -- and the publish action, which
    compares the two, has nothing to compare against.
    """
    rec["posted_comments"] = int(post.get("posted_comments") or 0)
    rec["posting_expected"] = int(post.get("expected_units") or 0)
    rec["post_ok"] = bool(post.get("post_ok"))
    rec["design_comment_posted"] = bool(post.get("design_comment_posted"))
    rec["posted_keys"] = list(post.get("posted_keys") or [])
    rec["posted_review_id"] = str(post.get("posted_review_id") or "")


def _all_delivered(per_change: list[dict]) -> bool:
    """True when every reviewed change delivered everything it expected to post.

    Mirrors the durable dedup index's own guard (posted >= expected) so the two
    cannot disagree about what "posted" means. A change that was cancelled, or
    that recorded no result, has nothing to deliver and does not block the
    verdict; anything that TRIED to post must have succeeded.
    """
    for rec in per_change:
        if rec.get("cancelled") or not rec.get("result_recorded"):
            continue
        if not rec.get("post_ok"):
            return False
        if int(rec.get("posted_comments") or 0) < int(rec.get("posting_expected") or 0):
            return False
    return True


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
