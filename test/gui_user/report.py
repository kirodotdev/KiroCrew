"""Render ``summary.json`` for humans: ``verdict.md``, the PR comment, the nightly issue.

Kept free of harness imports so the workflow can call it after the harness has
already exited (``python test/gui_user/report.py --summary ... --format comment``)
and so its formatting is unit-testable from a dict.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

COMMENT_MARKER = "<!-- gui-user-test -->"
REVIEWED_MARKER = "[GUI-USER-TESTED]"

#: Longest model-authored excerpt a comment carries.
MAX_MODEL_TEXT = 1200

_BADGE = {
    "PASS": "✅ PASS",
    "FAIL": "❌ FAIL",
    "ERROR": "⚠️ ERROR",
    "SKIPPED": "⏭️ SKIPPED",
}


def overall(summary: dict[str, Any]) -> str:
    statuses = [s.get("status") for s in summary.get("scenarios", [])]
    if not statuses:
        return "ERROR"
    if all(s == "PASS" for s in statuses):
        return "PASS"
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if any(s == "ERROR" for s in statuses):
        return "ERROR"
    return "SKIPPED"


def _last_attempt(sc: dict[str, Any]) -> dict[str, Any]:
    attempts = sc.get("attempts") or []
    return attempts[-1] if attempts else {}


def scenario_line(sc: dict[str, Any]) -> str:
    """One table row per scenario: status, name, steps, seconds, attempts, cost."""
    last = _last_attempt(sc)
    attempts = len(sc.get("attempts") or [])
    steps = last.get("steps", 0)
    secs = last.get("seconds", 0)
    usd = sum(float(a.get("usd", 0) or 0) for a in sc.get("attempts") or [])
    detail = last.get("status", "")
    if last.get("error"):
        detail = f"{detail}: {neutralize(str(last['error']), max_chars=80).replace('|', '/')}"
    return (
        f"| {_BADGE.get(sc.get('status', ''), sc.get('status', ''))} | `{sc.get('name')}` | {sc.get('tier')} "
        f"| {steps} | {secs}s | {attempts} | ${usd:.2f} | {detail} |"
    )


def neutralize(text: str, *, max_chars: int = MAX_MODEL_TEXT) -> str:
    """Make model-authored text inert for a bot-authored GitHub comment.

    The model's report is DERIVED from what it saw on screen, so anything a page
    could inject rides along. The text is only ever rendered inside a fenced
    code block, where Markdown, HTML, links and @-mentions are already literal;
    what remains is BREAKING OUT of that block, so fence delimiters are
    defanged, and the size is capped so a runaway report cannot flood the
    comment. Control characters (other than newline/tab) are dropped.
    """
    kept = [ch for ch in text if ch in "\n\t" or (ch.isprintable() and ch not in "\r\x0b\x0c")]
    cleaned = "".join(kept).replace("```", "'''").replace("~~~", "'''")
    # "@name" is what turns a word into a mention; GitHub does not notify from
    # inside a code fence, but the zero-width space keeps the text inert even if
    # a renderer ever unwraps the fence.
    cleaned = cleaned.replace("@", "@\u200b")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…"
    return cleaned


def _fenced(text: str) -> str:
    return "```text\n" + neutralize(text) + "\n```"


def _last_reported_attempt(sc: dict[str, Any]) -> dict[str, Any]:
    """The newest attempt that actually said something (final text or error)."""
    for att in reversed(sc.get("attempts") or []):
        if (att.get("final_text") or "").strip() or att.get("error"):
            return att
    return _last_attempt(sc)


def _final_text_blocks(summary: dict[str, Any]) -> list[str]:
    """Model-authored text, always inside a neutralized fence (see :func:`neutralize`)."""
    out: list[str] = []
    for sc in summary.get("scenarios", []):
        last = _last_reported_attempt(sc)
        text = (last.get("final_text") or "").strip()
        # Scenario names are repo-authored slugs, but they still pass the same gate.
        name = neutralize(str(sc.get("name", "")), max_chars=80).replace("`", "")
        if sc.get("status") == "PASS" and text:
            # A pass only needs its UI-ISSUES line, if any.
            issues = [ln for ln in text.splitlines() if ln.strip().upper().startswith("UI-ISSUES")]
            if issues and not issues[0].strip().lower().endswith("none"):
                out.append(
                    f"<details><summary><code>{name}</code> — UI issues noticed</summary>\n\n"
                    f"{_fenced(issues[0].strip())}\n\n</details>"
                )
            continue
        if text or last.get("error"):
            body = text
            if last.get("error"):
                body = f"Error: {last['error']}\n\n{body}".strip()
            out.append(
                f"<details><summary><code>{name}</code> — final report</summary>\n\n"
                f"{_fenced(body)}\n\n</details>"
            )
    return out


def render_markdown(
    summary: dict[str, Any], *, artifact_url: Optional[str] = None, run_url: Optional[str] = None
) -> str:
    """``verdict.md`` body (no marker, no header badge line)."""
    lines = [
        "| | Scenario | Tier | Steps | Time | Attempts | Cost | Detail |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [scenario_line(sc) for sc in summary.get("scenarios", [])]
    usage = summary.get("usage") or {}
    lines += [
        "",
        f"Model `{summary.get('model')}` · tool mode `{summary.get('tool_mode')}` · "
        f"{usage.get('calls', 0)} calls · {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out tokens · "
        f"≈ ${float(summary.get('usd', 0)):.2f} of ${float(summary.get('budget_usd', 0)):.2f} budget · "
        f"{summary.get('seconds', 0)}s wall.",
    ]
    if artifact_url or run_url:
        bits = []
        if artifact_url:
            bits.append(f"[screenshots + steps.jsonl]({artifact_url})")
        if run_url:
            bits.append(f"[workflow run]({run_url})")
        lines += ["", "Evidence: " + " · ".join(bits)]
    blocks = _final_text_blocks(summary)
    if blocks:
        lines += [""] + blocks
    return "\n".join(lines) + "\n"


def render_console(summary: dict[str, Any]) -> str:
    rows = []
    for sc in summary.get("scenarios", []):
        last = _last_attempt(sc)
        rows.append(
            f"  {sc.get('status', ''):7} {sc.get('name'):28} steps={last.get('steps', 0)} "
            f"t={last.get('seconds', 0)}s attempts={len(sc.get('attempts') or [])}"
        )
    return "\n".join(
        [
            f"GUI user test: {overall(summary)}  (${float(summary.get('usd', 0)):.2f}, mode {summary.get('tool_mode')})"
        ]
        + rows
    )


def render_comment(
    summary: dict[str, Any], *, head_sha: str, artifact_url: Optional[str], run_url: Optional[str]
) -> str:
    """Upserted PR comment. Advisory: the badge is information, not a gate."""
    verdict = overall(summary)
    return (
        "\n".join(
            [
                COMMENT_MARKER,
                f"## GUI user test (agentic, pixel-only) — {_BADGE.get(verdict, verdict)}",
                "",
                f"_A model drove a real browser on Xvfb through the `{summary.get('tier')}` scenarios against a seeded "
                f"gateway built from `{head_sha}`. Advisory — does not block merge. Updated in place on each run._",
                "",
                render_markdown(summary, artifact_url=artifact_url, run_url=run_url).rstrip(),
                "",
                f"{REVIEWED_MARKER} {head_sha}",
            ]
        )
        + "\n"
    )


def render_issue(
    summary: dict[str, Any], *, sha: str, run_url: Optional[str], artifact_url: Optional[str]
) -> tuple[str, str]:
    """(title, body) for the nightly failure issue."""
    verdict = overall(summary)
    failed = [sc["name"] for sc in summary.get("scenarios", []) if sc.get("status") != "PASS"]
    title = f"Nightly GUI user test {verdict}: {', '.join(failed) or 'no scenarios'}"
    body = "\n".join(
        [
            f"The nightly agentic GUI user test finished **{verdict}** on `main` at `{sha}`.",
            "",
            render_markdown(summary, artifact_url=artifact_url, run_url=run_url).rstrip(),
            "",
            "Open the artifact for per-step screenshots and `steps.jsonl`; a scenario that fails two nights in a row "
            "with the same final report is a real regression, one that flips is a flake to file against the scenario.",
            "",
            "Lane: `.github/workflows/gui-user-test.yml` · docs: `docs/build/gui-user-test.md` · tracking: #9578",
        ]
    )
    return title, body + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="render a GUI user-test summary.json")
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument(
        "--format",
        choices=("markdown", "comment", "issue-title", "issue-body", "verdict"),
        default="markdown",
    )
    p.add_argument("--head-sha", default="")
    p.add_argument("--run-url", default="")
    p.add_argument("--artifact-url", default="")
    args = p.parse_args(argv)
    try:
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read {args.summary}: {exc}", file=sys.stderr)
        return 2
    if args.format == "verdict":
        print(overall(summary))
    elif args.format == "comment":
        print(
            render_comment(
                summary,
                head_sha=args.head_sha,
                artifact_url=args.artifact_url or None,
                run_url=args.run_url or None,
            ),
            end="",
        )
    elif args.format in ("issue-title", "issue-body"):
        title, body = render_issue(
            summary,
            sha=args.head_sha,
            run_url=args.run_url or None,
            artifact_url=args.artifact_url or None,
        )
        print(
            title if args.format == "issue-title" else body,
            end="" if args.format == "issue-body" else "\n",
        )
    else:
        print(
            render_markdown(
                summary, artifact_url=args.artifact_url or None, run_url=args.run_url or None
            ),
            end="",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
