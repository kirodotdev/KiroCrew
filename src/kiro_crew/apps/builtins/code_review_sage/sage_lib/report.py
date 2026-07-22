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
import time
from pathlib import Path
from urllib.parse import urlparse

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/report.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import pipeline, results, store  # noqa: E402

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
_LLM_FINDING_FIELDS = ("observation", "consequence", "suggestion", "snippet")


def _redact_finding(f: dict) -> dict:
    out = dict(f)
    for k in _LLM_FINDING_FIELDS:
        if out.get(k):
            out[k] = pipeline._redact(str(out[k]))
    return out


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
        reason = (p1.get("band_override_reason") or "").strip() or "AI judgment"
        why = f"AI override -> {override} ({reason}) [baseline {band}: {why}]"
        band = override

    return {"band": band, "why": why, "score": focus_score(record)}


def build_report(records: list[dict], config: dict | None = None) -> dict:
    rows = []
    bands = {"red": 0, "yellow": 0, "green": 0}
    for rec in records:
        c = classify(rec, config)
        bands[c["band"]] += 1
        counts = rec.get("counts", {})
        rows.append({
            "change_id": rec.get("change_id", ""),
            "url": rec.get("url", ""),
            "title": pipeline._redact(str(rec.get("title", ""))),
            "platform": rec.get("platform", ""),
            "band": c["band"], "why": c["why"], "score": c["score"],
            "design_risk": rec.get("phase1", {}).get("design_risk", "low"),
            "blast": rec.get("blast_radius", {}).get("rating", "SMALL"),
            "red": counts.get("red", 0), "yellow": counts.get("yellow", 0),
            "deep_reviewed": rec.get("deep_reviewed", False),
            "gate_verdict": rec.get("phase1", {}).get("gate_verdict", "PASS"),
            "design_headline": pipeline._redact(
                str(rec.get("phase1", {}).get("design_headline", ""))),
            "problem": pipeline._redact(str(rec.get("phase1", {}).get("problem", ""))),
            "why_it_matters": pipeline._redact(str(rec.get("phase1", {}).get("why_it_matters", ""))),
            "solution_assessment": pipeline._redact(
                str(rec.get("phase1", {}).get("solution_assessment", ""))),
            "rationale": pipeline._redact(str(rec.get("phase1", {}).get("rationale", ""))),
            "findings": [_redact_finding(f) for f in (rec.get("findings", []) or [])],
        })
    order = {"red": 0, "yellow": 1, "green": 2}
    rows.sort(key=lambda r: (order[r["band"]], -r["score"]))
    return {"bands": bands, "rows": rows,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------------------------------------------------------------------------
# HTML rendering (theme-aware, for the artifact)
# ---------------------------------------------------------------------------

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

def reports_dir(root: Path | None = None) -> Path:
    return store.data_dir(root) / "reports"


def write_outputs(report: dict, html_body: str, root: Path | None = None,
                  slug: str | None = None) -> dict:
    """Write focus-report.html + rows.json + index.json (UI polls index.json)."""
    store.ensure_layout(root)
    rd = reports_dir(root)
    html_path = rd / "focus-report.html"
    html_path.write_text(html_body, encoding="utf-8")
    os.chmod(html_path, 0o600)
    # Compact 🔴+🟡 rows for inline rendering in the dashboard (chat/export default).
    focus_rows = [r for r in report["rows"] if r["band"] in ("red", "yellow")]
    rows_path = rd / "rows.json"
    rows_path.write_text(json.dumps(focus_rows, indent=2), encoding="utf-8")
    os.chmod(rows_path, 0o600)  # findings carry snippets from private diffs — match the HTML
    # Preserve a previously-set artifact slug when regenerating without one, so
    # "Open full report" keeps working across re-reviews (the driver calls
    # generate() with slug=None on every run).
    if slug is None:
        idx_path = rd / "index.json"
        if idx_path.exists():
            try:
                slug = json.loads(idx_path.read_text(encoding="utf-8")).get("report_slug")
            except (json.JSONDecodeError, OSError):
                slug = None
    index = {"report_slug": slug, "bands": report["bands"],
             "generated_at": report["generated_at"], "total": len(report["rows"])}
    idx_path = rd / "index.json"
    idx_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    os.chmod(idx_path, 0o600)
    return index


def set_report_slug(slug: str, root: Path | None = None) -> dict:
    """Record the artifact slug in index.json after the agent saves the artifact."""
    idx_path = reports_dir(root) / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {}
    idx["report_slug"] = slug
    idx_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")
    os.chmod(idx_path, 0o600)
    return idx


def reset(root: Path | None = None) -> None:
    """Clear the displayed Focus Report (index + rows) so a NEW review starts from
    a clean slate instead of showing the previous run's data. The previous run's
    report is already archived as an artifact (durable history), so this only
    clears the live display — not the record of past runs."""
    store.ensure_layout(root)
    rd = reports_dir(root)
    empty = {"report_slug": None, "bands": {"red": 0, "yellow": 0, "green": 0},
             "generated_at": "", "total": 0}
    idx_path = rd / "index.json"
    idx_path.write_text(json.dumps(empty, indent=2), encoding="utf-8")
    os.chmod(idx_path, 0o600)
    rows_path = rd / "rows.json"
    rows_path.write_text("[]", encoding="utf-8")
    os.chmod(rows_path, 0o600)


def generate(root: Path | None = None, slug: str | None = None) -> dict:
    """Read all result records, build + render + persist the report."""
    cfg = store.load_config(root)
    records = results.list_results(root)
    report = build_report(records, cfg)
    html_body = render_html(report)
    index = write_outputs(report, html_body, root, slug)
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
