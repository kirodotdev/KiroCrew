#!/usr/bin/env python3
"""Focus Report — the final pass that reads ALL result records and triages them
into bands (design §6).

Scoring + band assignment run deterministically here so the report is
reproducible and **every band carries a stored rationale** ("flagged: blast=LARGE
+ 2× 🔴"). The thresholds come from ``config.json:triage`` and are tunable
guidance; at runtime the reviewing AI may nudge a borderline change, but the
deterministic baseline is what this module emits. The HTML is saved as an
artifact by the agent; we also write ``reports/index.json`` for the UI to poll.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/report.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import pipeline, results, store  # noqa: E402

from kiro_crew.artifacts import _SLUG_RE  # noqa: E402

# Guarded reader, mirroring results.py's optional import: present in the
# runtime, absent when the app is driven standalone outside it.
try:
    from kiro_crew import hooks  # noqa: E402
except ImportError:  # pragma: no cover - standalone fallback
    hooks = None  # type: ignore

_RISK_W = {"low": 0, "medium": 35, "high": 60}
_BLAST_W = {"SMALL": 0, "MEDIUM": 25, "LARGE": 40}

# LLM-authored free-text fields. These are model output and must never be
# surfaced raw: the dashboard reads the local rows.json / focus-report.html this
# module writes DIRECTLY (no redaction in between), so we scrub here. Per
# untrusted-LLM-output guidance ("should not be trusted at all") + the security-controls
# guideline (scan with redact_exfiltration_urls + redact_credentials before any
# external surface). The artifact-archive path also redacts; this closes the
# local-file gap. ``pipeline._redact`` is a no-op when the redaction lib is
# unavailable (standalone), so this is safe everywhere.
_LLM_ROW_FIELDS = ("problem", "why_it_matters", "solution_assessment", "rationale")

# Nesting depth past which a value is stringified rather than walked. The record
# is worker-written JSON, so depth is attacker-chosen; recursing without a bound
# turns a deep payload into a RecursionError on the report path.
_REDACT_MAX_DEPTH = 6


def _redact_deep(value: object, skip: frozenset[str] = frozenset(),
                 _depth: int = 0) -> object:
    """Redact every string inside `value` -- keys and values, at any depth --
    preserving shape.

    A worker writes findings as free-form JSON, so every string in the structure is
    model-controlled: a nested dict, a list element, and a key name all reach
    report.json and the dashboard. Walking the whole structure is what makes the
    guarantee hold at depth; `skip` names are left alone at whatever level they
    appear, and non-string scalars (ints, bools, None) are returned as-is.

    Keys are redacted too, not just values. A worker writes findings as free-form
    JSON, so it controls key names as well as contents -- `{"evidence": {"<secret>":
    "seen here"}}` put the secret in the KEY, which reached report.json and the
    dashboard verbatim while the value beside it was scrubbed. `skip` is matched
    against the ORIGINAL key, so a skipped key keeps both its name and its value.

    Two keys that redact to the same placeholder collapse into one. That is the
    correct direction: losing a duplicate key in a report is recoverable, emitting a
    credential is not.

    Past `_REDACT_MAX_DEPTH` the value is stringified and redacted rather than
    walked further, so the result is still scrubbed but the recursion is bounded.
    """
    if isinstance(value, str):
        return pipeline._redact(value) if value else value
    if _depth >= _REDACT_MAX_DEPTH:
        return pipeline._redact(str(value))
    if isinstance(value, dict):
        return {_redact_key(k, skip): (v if k in skip
                                       else _redact_deep(v, skip, _depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v, skip, _depth + 1) for v in value]
    return value


def _redact_key(key: object, skip: frozenset[str]) -> object:
    """Scrub a mapping key, leaving `skip` names and non-string keys untouched.

    Split out so the two comprehensions that build redacted dicts cannot drift
    apart on how a key is treated -- one of them missing this is what let a
    credential-shaped key through.
    """
    if not isinstance(key, str) or key in skip:
        return key
    return pipeline._redact(key) if key else key


def _redact_deep_map(d: dict, skip: frozenset[str] = frozenset(), *,
                     redact_keys: bool = False) -> dict:
    """`_redact_deep` for a dict, typed as a dict so callers keep their shape.

    `redact_keys` says whether THIS dict's own key names are model-written. It is
    off by default because the caller that builds a report row supplies its own
    schema names (`band`, `score`, `change_id`), and scrubbing those would rewrite
    the report's structure rather than its contents. A caller whose top-level keys
    come from the worker passes it on. Keys nested INSIDE a value are always
    scrubbed -- `_redact_deep` owns those, and they are worker-controlled at every
    depth regardless of who owns this level.
    """
    return {(_redact_key(k, skip) if redact_keys else k):
            (v if k in skip else _redact_deep(v, skip, 1))
            for k, v in d.items()}


def _redact_finding(f: dict) -> dict:
    """Scrub every model-written string in a finding, at any depth.

    Enumerating the prose fields missed `file`, which the reviewer also writes and
    which reaches report.json and the dashboard. Redacting every string value
    instead of a named subset closed the set of KEYS; `_redact_deep` then closed
    the set of VALUES, so a nested dict or list can no longer carry an injected
    secret through.

    There is NO skip set. `line` used to be exempt "because it is numeric", but
    nothing enforced that: the boundary validator accepts any scalar, so a
    reviewer writing a credential into `line` as a string sailed past the
    exemption and reached the dashboard unredacted. `validate_result` now requires
    `line` to be a real number, which makes the exemption unnecessary — a number
    is not a string, so `_redact_deep` leaves it alone anyway. An exemption whose
    premise is not enforced is just a hole, so the premise is enforced and the
    exemption is gone.
    A finding's KEY NAMES are model-written too -- the boundary validator requires
    the fields it needs but does not forbid extras, so a worker can name a field
    anything, including a credential. `redact_keys=True` scrubs this level's names;
    nested keys are scrubbed unconditionally by `_redact_deep`.
    """
    return _redact_deep_map(f, redact_keys=True)


def focus_score(record: dict) -> int:
    p1 = record.get("phase1", {})
    risk = _RISK_W.get(p1.get("design_risk", "low"), 0)
    blast = _BLAST_W.get(record.get("blast_radius", {}).get("rating", "SMALL"), 0)
    counts = record.get("counts", {})
    finds = counts.get("red", 0) * 15 + counts.get("yellow", 0) * 5
    return min(100, risk + blast + finds)


def classify(record: dict, config: dict | None = None) -> dict:
    """Assign a band + a human-readable 'why flagged' rationale (design §6 rubric)."""
    cfg = (config or {}).get("triage", {})
    crit_blast = cfg.get("critical_blast", "LARGE")
    med_blast = cfg.get("medium_blast", "MEDIUM")
    yellow_min = cfg.get("yellow_min_yellow_findings", 2)

    p1 = record.get("phase1", {})
    verdict = p1.get("gate_verdict", "PASS")
    risk = p1.get("design_risk", "low")
    blast = record.get("blast_radius", {}).get("rating", "SMALL")
    counts = record.get("counts", {})
    red, yellow = counts.get("red", 0), counts.get("yellow", 0)
    branch = record.get("branch_gate_violation", False)
    regression = record.get("regression_detected", False)

    red_reasons = []
    if verdict == "BLOCK":
        red_reasons.append("design=BLOCK")
    if risk == "high":
        red_reasons.append("design risk high")
    if blast == crit_blast:
        red_reasons.append(f"blast={blast}")
    if red >= 1:
        red_reasons.append(f"{red}× 🔴")
    if branch:
        red_reasons.append("branch-gate violation")
    if regression:
        red_reasons.append("regression detected")

    if red_reasons:
        band, why = "red", " + ".join(red_reasons)
    else:
        yellow_reasons = []
        if risk == "medium":
            yellow_reasons.append("design risk medium")
        if blast == med_blast:
            yellow_reasons.append(f"blast={blast}")
        if yellow >= yellow_min:
            yellow_reasons.append(f"{yellow}× 🟡")
        if yellow_reasons:
            band, why = "yellow", " + ".join(yellow_reasons)
        else:
            band, why = "green", "low risk · small blast · no surviving findings"

    # §6: band assignment is AI judgment with the rubric as guidance. The
    # deterministic result above is the reproducible baseline; the reviewing AI
    # may nudge a borderline change by setting phase1.band_override (+ reason),
    # which is honored here and recorded so the override stays explainable.
    override = p1.get("band_override")
    if override in ("red", "yellow", "green") and override != band:
        # LLM-authored free text, and it is the only model-written part of `why`
        # — every other row field is redacted in build_report, so scrub it here
        # or a credential in the reason reaches report.json and the UI.
        # str() before strip(), matching how build_report coerces every other
        # worker-authored phase1 string. validate_result now refuses a non-string
        # phase1 value outright, but classify() also runs on records that never
        # went through it (adopted from an earlier run, or built in a test), so
        # the render path does not rely on validation alone.
        reason = pipeline._redact(
            str(p1.get("band_override_reason") or "").strip()) or "AI judgment"
        why = f"AI override -> {override} ({reason}) [baseline {band}: {why}]"
        band = override

    return {"band": band, "why": why, "score": focus_score(record)}


# The three bands the app groups by. `classify()` only ever produces one of these
# and `band_override` is already constrained to them, so this is a vocabulary for
# screening the UNTRUSTED read path, not a constraint on our own output.
#
# It replaces a redaction exemption. `band` used to be the one row field skipped by
# `_redact_row`, on the argument that `bands[row["band"]]` here and
# `BAND_DOT[row.band]` in ReportView index on its exact value. The concern was real
# but the protection was wrong: redaction is shape-based, so red/yellow/green come
# back byte-identical and the indexing was never at risk — while the exemption made
# `band` the single field in the row that reached the dashboard verbatim, so a
# planted "red <credential>" leaked where the prose beside it was scrubbed.
#
# An earlier version of that set also exempted `change_id`, `platform`,
# `gate_verdict`, `blast` and `design_risk` on the claim that they are "validated
# against fixed vocabularies". That was wrong for the same reason: `validate_result`
# enforces a vocabulary for `gate_verdict` alone. Nothing in a row is exempt now.
_VALID_BANDS = frozenset({"red", "yellow", "green"})


def _redact_row(row: dict) -> dict:
    """Scrub every string in a report row, at any depth. Nothing is exempt.

    The whole result record is written by the reviewing worker, so any prose or URL
    it contains can carry injected content — `url` reached report.json unredacted
    while the prose fields beside it were scrubbed.

    `band` was the last exemption, on the argument that both `bands[]` and
    `BAND_DOT[]` index on its exact value. That was the wrong protection for the
    right concern: redaction is shape-based, so `red` / `yellow` / `green` come
    back byte-identical and the indexing is unaffected — while the exemption meant
    a planted `band` of "red <credential>" was the one field in the row that
    reached the dashboard verbatim. Keying is instead protected by admitting only
    the three known bands on the untrusted read path (see `read_report`), which is
    a vocabulary check rather than a redaction hole.

    Depth matters here for the same reason it does in `_redact_finding`: most of
    these values come straight off the record (`rec.get("url")`,
    `rec.get("platform")`, ...), so a worker that wrote one as a dict or list
    slipped it past an `isinstance(v, str)` test and into the report. Non-strings
    that are genuinely scalar (counts, scores, booleans) are returned untouched,
    and redaction is idempotent for values already scrubbed above.
    """
    return _redact_deep_map(row)


def build_report(records: list[dict], config: dict | None = None) -> dict:
    # Rows are paired with the band `classify()` computed, so ordering never reads
    # control-flow data back out of a redacted row. That coupling is what made the
    # old `band` redaction exemption load-bearing: `order[r["band"]]` on the
    # post-redaction row raises KeyError the moment redaction alters the value.
    paired: list[tuple[str, dict]] = []
    bands = {"red": 0, "yellow": 0, "green": 0}
    for rec in records:
        c = classify(rec, config)
        bands[c["band"]] += 1
        counts = rec.get("counts", {})
        paired.append((c["band"], _redact_row({
            "change_id": rec.get("change_id", ""),
            "url": rec.get("url", ""),
            "title": pipeline._redact(str(rec.get("title", ""))),
            "platform": rec.get("platform", ""),
            "band": c["band"], "why": c["why"], "score": c["score"],
            "design_risk": rec.get("phase1", {}).get("design_risk", "low"),
            "blast": rec.get("blast_radius", {}).get("rating", "SMALL"),
            "red": counts.get("red", 0), "yellow": counts.get("yellow", 0),
            "deep_reviewed": rec.get("deep_reviewed", False),
            # str() for the same reason as the fields below: the renderer
            # html.escape()s this, which raises on a non-string.
            "gate_verdict": str(rec.get("phase1", {}).get("gate_verdict", "PASS")),
            "design_headline": pipeline._redact(
                str(rec.get("phase1", {}).get("design_headline", ""))),
            "problem": pipeline._redact(str(rec.get("phase1", {}).get("problem", ""))),
            "why_it_matters": pipeline._redact(str(rec.get("phase1", {}).get("why_it_matters", ""))),
            "solution_assessment": pipeline._redact(
                str(rec.get("phase1", {}).get("solution_assessment", ""))),
            "rationale": pipeline._redact(str(rec.get("phase1", {}).get("rationale", ""))),
            # The EXACT body the ship-readiness comment would post. Carried on the
            # row so the app can show what it is about to send rather than a
            # placeholder — build_ship_comment is already _redact-scrubbed, and it
            # is the same call the poster uses, so the two cannot drift.
            "ship_comment": pipeline.build_ship_comment(rec),
            "findings": [_redact_finding(f) for f in (rec.get("findings", []) or [])],
        })))
    order = {"red": 0, "yellow": 1, "green": 2}
    paired.sort(key=lambda pr: (order[pr[0]], -pr[1]["score"]))
    rows = [row for _band, row in paired]
    return {"bands": bands, "rows": rows,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------------------------------------------------------------------------
# HTML rendering (theme-aware base + Apple "system" accent palette)
# ---------------------------------------------------------------------------
# Base surface/text stay on the dashboard theme vars (so the report adapts to
# light/dark/custom), while severity + accent use Apple's system semantic
# colours, which read well on both. Apple design cues: SF font stack, generous
# whitespace, hairline separators, tinted rounded pills, restrained palette.
_SEV_COLORS = {
    "red": ("#FF3B30", "rgba(255,59,48,.12)"),     # systemRed
    "yellow": ("#FF9500", "rgba(255,149,0,.14)"),  # systemOrange
    "green": ("#34C759", "rgba(52,199,89,.14)"),   # systemGreen
}
_LINK = "#0A84FF"   # systemBlue
_FONT = ("-apple-system,BlinkMacSystemFont,'SF Pro Text','SF Pro Display',"
         "system-ui,'Segoe UI',Roboto,sans-serif")
_NEUTRAL_BG = "rgba(127,127,127,.12)"


def _pill(text: str, fg: str, bg: str) -> str:
    return (f"<span style='background:{bg};color:{fg};padding:2px 9px;border-radius:9999px;"
            f"font-size:11px;font-weight:600;letter-spacing:.01em;white-space:nowrap'>"
            f"{html.escape(text)}</span>")


def _dot(color: str) -> str:
    return (f"<span style='display:inline-block;width:8px;height:8px;border-radius:50%;"
            f"background:{color};margin-right:8px;vertical-align:middle'></span>")


def _finding_html(f: dict) -> str:
    e = html.escape
    color, _bg = _SEV_COLORS["red" if f.get("severity") == "red" else "yellow"]
    loc = e(str(f.get("file", "")))
    if f.get("line"):
        loc += f":{e(str(f.get('line')))}"
    snip = f.get("snippet", "")
    snip_html = (
        "<pre style='margin:8px 0 0;padding:10px 12px;background:var(--bg);"
        "border:1px solid var(--border);border-radius:8px;overflow:auto;font-size:11.5px;"
        "line-height:1.45;white-space:pre-wrap;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace'>"
        f"{e(snip)}</pre>" if snip else "")
    return (
        f"<div style='padding:12px 14px;margin:10px 0;background:var(--bg);border:1px solid "
        f"var(--border);border-left:3px solid {color};border-radius:10px'>"
        f"<div style='font-size:13px;font-weight:600'>{_dot(color)}{e(str(f.get('dimension', '')))}"
        f"<span style='color:var(--muted);font-weight:400'> · {loc}</span></div>"
        f"<div style='font-size:13px;line-height:1.5;margin-top:6px'>{e(str(f.get('observation', '')))}</div>"
        f"<div style='font-size:12px;color:var(--muted);line-height:1.5;margin-top:4px'>"
        f"&#8627; {e(str(f.get('consequence', '')))}</div>"
        f"{snip_html}"
        f"<div style='font-size:12.5px;line-height:1.5;margin-top:8px'>"
        f"<span style='color:{_LINK};font-weight:600'>Suggestion</span> &nbsp;"
        f"{e(str(f.get('suggestion', '')))}</div>"
        "</div>"
    )


def _design_facets(val: object) -> list[str]:
    """Split a design-section value into scannable lines. Honors explicit
    newlines (the gate now emits short labeled facets); if it's a single long
    prose blob (records predating the structured prompt), splits into sentences
    so it doesn't render as one dense, hard-to-read paragraph. The value is
    redacted (idempotent — ``build_report`` is the primary chokepoint) so this
    render helper never emits un-scrubbed LLM text to the dashboard surface."""
    s = pipeline._redact(str(val)).strip()
    parts = [p.strip(" \t-•") for p in s.splitlines() if p.strip()]
    if len(parts) <= 1 and len(s) > 160:
        parts = [seg.strip() for seg in re.split(r"(?<=[.!?])\s+", s) if seg.strip()]
    return parts or ([s] if s else [])


def _facet_html(line: str) -> str:
    """Render one facet line; bold a leading ``Label:`` prefix when present so
    facets like ``Tradeoffs: …`` read as a labeled list."""
    e = html.escape
    m = re.match(r"^([A-Z][\w /&'+-]{1,40}):\s+(.*)$", line)
    if m:
        return ("<div style='font-size:13px;line-height:1.5;margin:3px 0'>"
                f"<strong>{e(m.group(1))}:</strong> {e(m.group(2))}</div>")
    return f"<div style='font-size:13px;line-height:1.5;margin:3px 0'>{e(line)}</div>"


def _design_html(r: dict) -> str:
    """The design narrative as a scannable chain: customer problem -> why it
    matters -> does the design resolve it / at what cost. Apple-style: small
    uppercase secondary labels over readable body. Each section's content is
    broken into discrete facet lines (newline-separated, or sentence-split for
    legacy prose) so a long ``solution_assessment`` reads as a scannable list
    instead of one dense paragraph. Falls back to the freeform rationale for
    records predating the structured fields."""
    e = html.escape
    out = []
    # Lead line: the direct description of the design issue the author actually acts on
    # (same text posted as the draft comment). The chain below is supporting depth.
    headline = str(r.get("design_headline", "")).strip()
    if headline:
        out.append(
            "<div style='font-size:13.5px;font-weight:600;line-height:1.5;"
            "margin:0 0 12px'>" + e(headline) + "</div>")
    steps = [("Problem", r.get("problem")),
             ("Why it matters", r.get("why_it_matters")),
             ("Solution fit", r.get("solution_assessment"))]
    steps = [(lbl, val) for lbl, val in steps if val]
    if steps:
        for lbl, val in steps:
            facets = "".join(_facet_html(line) for line in _design_facets(val))
            out.append(
                "<div style='margin:0 0 12px'>"
                "<div style='font-size:10.5px;font-weight:600;color:var(--muted);"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px'>"
                f"{lbl}</div>"
                f"{facets}</div>")
        return "".join(out)
    if out:                       # headline present but no chain (older/short records)
        return "".join(out)
    if r.get("rationale"):
        return "".join(_facet_html(line) for line in _design_facets(r["rationale"]))
    return ""


def _detail_html(r: dict) -> str:
    """Collapsible design chain-of-thought + each finding's full detail."""
    design = _design_html(r)
    findings = "".join(_finding_html(f) for f in (r.get("findings") or []))
    if not design and not findings:
        return ""
    n = len(r.get("findings") or [])
    label = (f"Design reasoning + {n} finding{'s' if n != 1 else ''}"
             if n else "Design reasoning")
    verdict = html.escape(r["gate_verdict"])
    vc, vbg = (_SEV_COLORS["red"] if verdict == "BLOCK"
               else _SEV_COLORS["yellow"] if verdict == "CONCERNS"
               else _SEV_COLORS["green"])
    body = ""
    if design:
        body += (
            "<div style='margin:10px 0;padding:14px 16px;background:var(--bg);"
            "border:1px solid var(--border);border-radius:10px'>"
            "<div style='font-size:10.5px;font-weight:600;text-transform:uppercase;"
            "letter-spacing:.06em;color:var(--muted);margin-bottom:8px'>Design gate"
            f" &nbsp;{_pill(verdict, vc, vbg)}</div>{design}</div>")
    body += findings
    return (f"<details style='margin-top:8px'><summary style='cursor:pointer;font-size:12.5px;"
            f"font-weight:500;color:{_LINK}'>{label}</summary>{body}</details>")


def _safe_href(url: object) -> str:
    """Allowlist http(s) for a link rendered in the report HTML. The PR URL is
    LLM/PR-derived; output-encoding (html.escape) neutralizes quotes but NOT a
    ``javascript:``/``data:`` scheme, so a scheme allowlist is required for the
    ``href`` attribute context (href/XSS guidance — default-deny). A
    non-http(s) URL collapses to ``#``."""
    try:
        if urlparse(str(url)).scheme.lower() in ("http", "https"):
            return str(url)
    except Exception:
        pass
    return "#"


def _row_html(r: dict) -> str:
    e = html.escape
    rc, rbg = _SEV_COLORS["red"]
    yc, ybg = _SEV_COLORS["yellow"]
    counts = []
    if r["red"]:
        counts.append(_pill(f"{r['red']} blocking", rc, rbg))
    if r["yellow"]:
        counts.append(_pill(f"{r['yellow']} should-fix", yc, ybg))
    badges = (f"{_pill('design ' + str(r['design_risk']), 'var(--muted)', _NEUTRAL_BG)} "
              f"{_pill('blast ' + str(r['blast']), 'var(--muted)', _NEUTRAL_BG)}")
    link = (f"<a href='{e(_safe_href(r['url']))}' target='_blank' rel='noopener noreferrer' "
            f"style='color:{_LINK};text-decoration:none;font-weight:600'>{e(r['change_id'])}</a>")
    gate = ("" if r["deep_reviewed"] else
            " <span style='color:var(--muted);font-style:italic'>· gate only — deep review incomplete</span>")
    return (
        "<div style='padding:16px 0;border-bottom:1px solid var(--border)'>"
        "<div style='display:flex;justify-content:space-between;align-items:baseline;gap:12px'>"
        f"<div style='font-size:14px;line-height:1.4'>{link} &nbsp;"
        f"<span style='font-weight:600'>{e(r['title'])}</span>{gate}</div>"
        f"<span style='color:var(--muted);font-size:12px;white-space:nowrap'>score {r['score']}</span></div>"
        "<div style='margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center'>"
        f"{badges} {' '.join(counts)}</div>"
        f"<div style='font-size:12px;color:var(--muted);margin-top:6px'>{e(r['why'])}</div>"
        f"{_detail_html(r)}"
        "</div>"
    )


def render_html(report: dict) -> str:
    b = report["bands"]
    rows = report["rows"]
    red = [r for r in rows if r["band"] == "red"]
    yellow = [r for r in rows if r["band"] == "yellow"]
    green = [r for r in rows if r["band"] == "green"]
    rc, rbg = _SEV_COLORS["red"]
    yc, ybg = _SEV_COLORS["yellow"]
    gc, gbg = _SEV_COLORS["green"]

    def section(label, color, items, open_=True):
        if not items:
            return ""
        body = "".join(_row_html(r) for r in items)
        op = " open" if open_ else ""
        return (f"<details{op} style='margin-top:20px'>"
                f"<summary style='cursor:pointer;font-size:13px;font-weight:600;padding:4px 0'>"
                f"{_dot(color)}{label}</summary>{body}</details>")

    parts = [
        f"<div style='font-family:{_FONT};color:var(--text);width:100%;"
        "box-sizing:border-box;padding:8px 18px 18px'>",
        ("<div style='display:flex;justify-content:space-between;align-items:baseline;"
         "border-bottom:1px solid var(--border);padding-bottom:12px'>"
         "<h2 style='margin:0;font-size:22px;font-weight:700;letter-spacing:-.021em'>"
         "Focus Report</h2>"
         f"<span style='font-size:12px;color:var(--muted)'>{html.escape(report['generated_at'])}</span></div>"),
        ("<div style='display:flex;flex-wrap:wrap;gap:6px;align-items:center;"
         "font-size:13px;color:var(--muted);margin-top:12px'>"
         "<span>Look here first.</span>"
         f"{_pill(str(b['red']) + ' needs review', rc, rbg)}"
         f"{_pill(str(b['yellow']) + ' worth a glance', yc, ybg)}"
         f"{_pill(str(b['green']) + ' clean', gc, gbg)}</div>"),
        section(f"Needs review ({b['red']})", rc, red, open_=True),
        section(f"Worth a glance ({b['yellow']})", yc, yellow, open_=False),
    ]
    if green:
        parts.append(
            "<details style='margin-top:20px'>"
            "<summary style='cursor:pointer;color:var(--muted);font-size:12.5px;padding:4px 0'>"
            f"{_dot(gc)}{b['green']} clean — low risk, small blast, no findings</summary>"
            + "".join(_row_html(r) for r in green) + "</details>")
    if not rows:
        parts.append("<p style='color:var(--muted);margin-top:16px'>No reviewed changes yet.</p>")
    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def reports_dir(root: Path | None = None, run_id: str | None = None) -> Path:
    """Where a report is written. With ``run_id``, the run's private
    ``data/runs/<id>/report`` — so every finished run keeps its own report and the
    dashboard can render any of them, not just the newest. Without it, the legacy
    shared ``data/reports``."""
    if run_id:
        return store.run_dir(run_id, root) / "report"
    return store.data_dir(root) / "reports"


def _ensure(root: Path | None, run_id: str | None) -> None:
    if run_id:
        store.ensure_run_layout(run_id, root)
    else:
        store.ensure_layout(root)


def write_outputs(report: dict, html_body: str, root: Path | None = None,
                  slug: str | None = None, run_id: str | None = None) -> dict:
    """Write focus-report.html + rows.json + report.json + index.json.

    ``rows.json`` stays the compact 🔴+🟡 focus subset (what the chat/export
    surface renders). ``report.json`` is the FULL report — every band, every row,
    every finding — because the dashboard now renders the report inline and needs
    the clean rows (already redacted by ``build_report``) rather than scraping the
    HTML. ``index.json`` remains the small pointer the UI polls."""
    _ensure(root, run_id)
    rd = reports_dir(root, run_id)
    rd.mkdir(parents=True, exist_ok=True)
    html_path = rd / "focus-report.html"
    _atomic_write(html_path, html_body)
    # Compact 🔴+🟡 rows for inline rendering in the dashboard (chat/export default).
    focus_rows = [r for r in report["rows"] if r["band"] in ("red", "yellow")]
    rows_path = rd / "rows.json"
    # findings carry snippets from private diffs — 0600 like the HTML
    _atomic_write(rows_path, json.dumps(focus_rows, indent=2))
    # Full report for the in-app report view (all bands + findings).
    full_path = rd / "report.json"
    _atomic_write(full_path, json.dumps(report, indent=2))
    # Preserve a previously-set artifact slug when regenerating without one, so
    # "Open full report" keeps working across re-reviews (the driver calls
    # generate() with slug=None on every run).
    if slug is None:
        idx_path = rd / "index.json"
        if idx_path.exists():
            try:
                _raw = read_within_reports(idx_path, root, run_id)
                slug = json.loads(_raw or "{}").get("report_slug")
            except (json.JSONDecodeError, OSError):
                slug = None
    index = {"report_slug": slug, "bands": report["bands"],
             "generated_at": report["generated_at"], "total": len(report["rows"])}
    idx_path = rd / "index.json"
    _atomic_write(idx_path, json.dumps(index, indent=2))
    return index


def _safe_slug(value: object) -> str | None:
    """Return *value* only if it is a well-formed artifact slug, else None.

    `index.json` is worker-writable and its slug is turned into a dashboard share
    link, so the value is screened against the artifact store's own grammar
    (`artifacts._SLUG_RE`, which is what blocks path traversal there) instead of
    being trusted or merely redacted. Anything that is not a slug cannot name a
    real artifact, so dropping it loses nothing.
    """
    if not isinstance(value, str):
        return None
    return value if _SLUG_RE.match(value) else None


def _as_count(value: object, default: int = 0) -> int:
    """Coerce a persisted tally to a non-negative int, falling back to *default*.

    Counts read back from `report.json` / `index.json` are worker-reachable, and the
    dashboard does arithmetic on them. A bool is rejected explicitly because it is
    an int subclass and `True` would silently read as 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= 0 else default


def read_report(root: Path | None = None, run_id: str | None = None) -> dict | None:
    """Load a persisted full report (``report.json`` + the index pointer), or None
    when the run has not produced one yet. This is the read path the dashboard's
    inline report view uses; it never touches the artifact store."""
    rd = reports_dir(root, run_id)
    full = rd / "report.json"
    if not full.is_file():
        return None
    try:
        _raw = read_within_reports(full, root, run_id)
        if _raw is None:
            return None
        report = json.loads(_raw)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(report, dict):
        return None
    idx: dict = {}
    idx_path = rd / "index.json"
    if idx_path.is_file():
        try:
            loaded = json.loads(read_within_reports(idx_path, root, run_id)
                                or "{}")
            if isinstance(loaded, dict):
                idx = loaded
        except (json.JSONDecodeError, OSError):
            idx = {}
    rows = report.get("rows") or []
    # Redact on READ, not just on write. `read_within_reports` refuses symlinks and
    # paths escaping the reports dir, but the dir itself is writable by the review
    # worker, so a REAL report.json planted there is a legitimate file as far as the
    # reader is concerned -- and its rows went straight to the dashboard with none
    # of the redaction `build_report` applies on the way in. Re-running the same
    # `_redact_row` here is idempotent, so a report this module actually built is
    # unchanged, while planted content is scrubbed before it can reach the UI.
    #
    # Non-dict rows are dropped rather than passed through: a row is a mapping by
    # contract, anything else cannot render, and there is no safe way to redact it.
    # A row is a mapping by contract, and its `band` must be one of the three the
    # app groups by. Now that redaction scrubs `band` like any other worker-written
    # string, this vocabulary check is what keeps `bands[]` / `BAND_DOT[]` keying
    # intact -- and it stops a planted row inventing a fourth grouping key. Rows
    # failing either test are dropped: a row that cannot be grouped cannot render.
    rows = [_redact_row(dict(r)) for r in rows
            if isinstance(r, dict) and r.get("band") in _VALID_BANDS]
    # The band tallies and total are counts. A planted file can put any JSON here,
    # and the UI does arithmetic on them, so coerce to int and fall back to 0 --
    # same shape-not-name treatment `counts` already gets in validate_result.
    #
    # `raw_bands` must be screened for being a MAPPING before `.get`, not merely
    # for truthiness: `[] or {}` happens to yield `{}`, but a non-empty list, a
    # string or a number is truthy and `.get` raises AttributeError, turning a
    # planted file into an HTTP 500 on the report endpoint.
    raw_bands = report.get("bands")
    if not isinstance(raw_bands, dict):
        raw_bands = {}
    bands = {k: _as_count(raw_bands.get(k)) for k in ("red", "yellow", "green")}
    return {
        "bands": bands,
        "rows": rows,
        "generated_at": pipeline._redact(
            str(report.get("generated_at") or idx.get("generated_at") or "")),
        "total": _as_count(idx.get("total"), default=len(rows)),
        # The slug names an artifact the dashboard turns into a share link, and
        # index.json is worker-writable, so it is screened against the artifact
        # store's OWN slug grammar rather than merely redacted: a value that is
        # not a slug cannot be a valid artifact reference, so returning None is
        # both safer and truer than passing arbitrary text through. Reusing
        # `artifacts._SLUG_RE` keeps one definition of what a slug is.
        "report_slug": _safe_slug(idx.get("report_slug")),
    }


def set_report_slug(slug: str, root: Path | None = None,
                    run_id: str | None = None) -> dict:
    """Record the artifact slug in index.json after the agent saves the artifact."""
    idx_path = reports_dir(root, run_id) / "index.json"
    idx = json.loads(read_within_reports(idx_path, root, run_id) or "{}")
    idx["report_slug"] = slug
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(idx_path, json.dumps(idx, indent=2))
    return idx


def reset(root: Path | None = None, run_id: str | None = None) -> None:
    """Clear the displayed Focus Report (index + rows) so a NEW review starts from
    a clean slate instead of showing the previous run's data. The previous run's
    report is already archived as an artifact (durable history), so this only
    clears the live display — not the record of past runs."""
    _ensure(root, run_id)
    rd = reports_dir(root, run_id)
    rd.mkdir(parents=True, exist_ok=True)
    empty = {"report_slug": None, "bands": {"red": 0, "yellow": 0, "green": 0},
             "generated_at": "", "total": 0}
    idx_path = rd / "index.json"
    _atomic_write(idx_path, json.dumps(empty, indent=2))
    rows_path = rd / "rows.json"
    _atomic_write(rows_path, "[]")


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, without following a symlink there.

    The reports dir is reachable by the review worker, so any of these output
    names can be a planted symlink; `write_text` would follow it and overwrite
    the linked file, and the `os.chmod` after it would follow it too. Staging a
    private temp file in the same directory and renaming over the name swaps the
    NAME without following a link, so a plant is destroyed rather than honoured.
    Mirrors `learning.py:_atomic_write`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)  # always close the fd, even if os.write raised
        os.chmod(tmp, 0o600)  # 0600 before it takes the real name, not after
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_within_reports(path: Path, root: Path | None = None,
                        run_id: str | None = None) -> str | None:
    """Read a text file from the reports dir without following a link there.

    The counterpart to `_atomic_write`, and for the same reason: the reports dir
    is reachable by the review worker, so any of these names can be a planted
    symlink. Round 21 stopped the WRITES from following a plant, which left the
    reads — a link at `focus-report.html` or `index.json` pointing at a
    credential file was still followed, and its contents flowed onward into a
    dashboard artifact or a rendered report. `hooks.safe_read_file_bytes_nolink`
    opens with O_NOFOLLOW and validates the inode it actually read, and pinning
    `within_root` to the reports dir also rejects a path that escapes it.
    Returns None when the file is missing, planted, or not valid UTF-8; every
    caller already treats None as "no report".
    """
    rd = reports_dir(root, run_id)
    if hooks is None:  # pragma: no cover - standalone fallback
        try:
            raw: bytes | None = path.read_bytes()
        except OSError:
            return None
    else:
        raw = hooks.safe_read_file_bytes_nolink(str(path), str(rd))
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def generate(root: Path | None = None, slug: str | None = None,
             run_id: str | None = None) -> dict:
    """Read all result records, build + render + persist the report."""
    cfg = store.load_config(root)
    records = results.list_results(root, run_id)
    report = build_report(records, cfg)
    html_body = render_html(report)
    index = write_outputs(report, html_body, root, slug, run_id)
    return {"index": index, "html": html_body, "report": report}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage Focus Report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    gp = sub.add_parser("generate")
    gp.add_argument("--slug", default=None)
    sp = sub.add_parser("set-slug")
    sp.add_argument("slug")
    args = ap.parse_args(argv)
    if args.cmd == "generate":
        out = generate(slug=args.slug)
        print(json.dumps({"index": out["index"],
                          "html_file": str(reports_dir() / "focus-report.html")}, indent=2))
    elif args.cmd == "set-slug":
        print(json.dumps(set_report_slug(args.slug), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
