from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from kiro_crew.personal_insights.insights_canonical import (
    canonical_lesson_digest,
    canonical_text_digest,
)
from kiro_crew.personal_insights.insights_facts import SessionFacts

CTA_BY_CLASS = {
    "lesson_proposal": "Do it",
    "prompt": "Use this now",
    "steering_patch": "Review and do it",
    "existing_capability": "Run it",
}
EXECUTABLE_CLASSES = {"lesson_proposal"}
APPLIED_STATES = {"applied_verified", "changed_unverified"}
FOLLOW_THROUGH_MIN_SESSIONS = 5
FOLLOW_THROUGH_MATERIAL_POINTS = 10.0


def _started_epoch(facts: SessionFacts) -> float | None:
    raw = facts.started.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _signal_shares(group: list[SessionFacts]) -> dict[str, float | int]:
    count = len(group)
    corrected = sum(1 for f in group if f.correction_markers)
    stalled = sum(1 for f in group if f.stalls)
    return {
        "sessions": count,
        "corrected_share": round(100.0 * corrected / count, 1) if count else 0.0,
        "stalled_share": round(100.0 * stalled / count, 1) if count else 0.0,
    }


def follow_through(
    prior_actions: list[dict[str, Any]], facts: list[SessionFacts]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for prior in prior_actions:
        applied_at = prior.get("applied_at")
        before: list[SessionFacts] = []
        since: list[SessionFacts] = []
        if isinstance(applied_at, (int, float)):
            for item in facts:
                started = _started_epoch(item)
                if started is None:
                    continue
                (since if started >= applied_at else before).append(item)
        before_shares = _signal_shares(before)
        since_shares = _signal_shares(since)
        verdict = "too_early"
        delta = 0.0
        if len(since) >= FOLLOW_THROUGH_MIN_SESSIONS and before:
            delta = float(since_shares["corrected_share"]) - float(before_shares["corrected_share"])
            if delta <= -FOLLOW_THROUGH_MATERIAL_POINTS:
                verdict = "improved"
            elif delta >= FOLLOW_THROUGH_MATERIAL_POINTS:
                verdict = "worse"
            else:
                verdict = "unchanged"
        out.append(
            {
                "action_id": prior["action_id"],
                "run_id": prior["run_id"],
                "title": prior["title"],
                "state": prior["state"],
                "applied_at": applied_at,
                "behavior_predicate": prior.get("behavior_predicate"),
                "before": before_shares,
                "since": since_shares,
                "sessions_needed": max(0, FOLLOW_THROUGH_MIN_SESSIONS - len(since)),
                "delta_points": round(delta, 1),
                "verdict": verdict,
            }
        )
    return out


def _follow_through_lines(item: dict[str, Any]) -> list[str]:
    applied = item.get("applied_at")
    when = (
        datetime.fromtimestamp(applied, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if isinstance(applied, (int, float))
        else "unknown time"
    )
    lines = [f"### {item['title']}", "", f"Applied {when} (run `{item['run_id']}`)."]
    since = item["since"]
    before = item["before"]
    if item["verdict"] == "too_early":
        lines.append(
            f"Too early to judge: {since['sessions']} sessions since it was applied, "
            f"{item['sessions_needed']} more needed before a comparison is reported."
        )
    else:
        label = {
            "improved": "Improved",
            "unchanged": "No measurable change yet",
            "worse": "Worse",
        }[item["verdict"]]
        lines.append(
            f"{label}: sessions with owner corrections went from {before['corrected_share']}% "
            f"({before['sessions']} sessions before) to {since['corrected_share']}% "
            f"({since['sessions']} sessions since). Stalls: {before['stalled_share']}% to "
            f"{since['stalled_share']}%."
        )
    lines.append("")
    lines.append("**Keep it**  ·  Refine it  ·  **Undo it**")
    lines.append("")
    return lines


def _tier(count: int) -> str:
    if count >= 6:
        return "usually"
    if count >= 4:
        return "recurring"
    if count >= 2:
        return "repeated"
    return "single"


def _session_link(key: str, facts_by_key: dict[str, SessionFacts]) -> str:
    facts = facts_by_key.get(key)
    title = facts.title if facts else key
    return f"[{title}](/sessions/{key})"


def _evidence_line(keys: list[str], total: int, facts_by_key: dict[str, SessionFacts]) -> str:
    keys = [k for k in keys if k in facts_by_key]
    links = ", ".join(_session_link(k, facts_by_key) for k in keys[:4])
    more = f" and {len(keys) - 4} more" if len(keys) > 4 else ""
    return f"{_tier(len(keys))}: {len(keys)} of {total} analyzed sessions. {links}{more}"


_KEY_RE = re.compile(r"dashboard_chat-\d+-\d+")


def _link_prose(text: str, facts_by_key: dict[str, SessionFacts]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(0)
        return _session_link(key, facts_by_key) if key in facts_by_key else "an unlisted session"

    return _KEY_RE.sub(repl, text)


def applied_action_keys(prior_actions: list[dict[str, Any]]) -> set[str]:
    return {
        str(p["action_key"])
        for p in prior_actions
        if p.get("action_key") and p.get("state") in APPLIED_STATES
    }


def build_actions(
    synthesis: dict[str, Any], prior_actions: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    applied = applied_action_keys(prior_actions or [])
    for rec in synthesis.get("recommendations", []):
        artifact = rec.get("artifact") or {}
        if rec["action_class"] == "lesson_proposal":
            fields = {
                "rule": (artifact.get("rule") or "").strip(),
                "negative": (artifact.get("negative") or "").strip(),
                "scope": "global",
            }
            digest = canonical_lesson_digest(fields)
            payload: dict[str, Any] = {"kind": "structured_tool", "structured_parameters": fields}
            display = fields["rule"] + (
                f"\nNOT: {fields['negative']}" if fields["negative"] else ""
            )
        else:
            text = (artifact.get("text") or artifact.get("rule") or "").strip()
            digest = canonical_text_digest(text)
            payload = {"kind": "text", "content": text}
            display = text
        action_key = hashlib.sha256(
            f"{rec.get('behavior_predicate')}|{rec['action_class']}|global".encode("utf-8")
        ).hexdigest()[:16]
        if action_key in applied:
            continue
        actions.append(
            {
                "action_id": f"{rec['action_id']}-{digest[:8]}",
                "action_key": action_key,
                "rank": len(actions) + 1,
                "title": rec["title"],
                "action_class": rec["action_class"],
                "behavior_predicate": rec.get("behavior_predicate"),
                "why": rec.get("why", ""),
                "claim_ids": rec.get("claim_ids", []),
                "artifact": payload,
                "artifact_digest": digest,
                "display_artifact": display,
                "expected_observation": rec.get("expected_observation", ""),
                "verification": rec.get("verification", ""),
                "undo": rec.get("undo", ""),
                "cta": CTA_BY_CLASS[rec["action_class"]],
                "executable": rec["action_class"] in EXECUTABLE_CLASSES,
            }
        )
    return actions


def _claim_keys(synthesis: dict[str, Any], claim_ids: list[str]) -> list[str]:
    keys: list[str] = []
    for section in ("friction", "working"):
        for claim in synthesis.get(section, []):
            if claim.get("claim_id") in claim_ids:
                keys.extend(claim.get("session_keys", []))
    return list(dict.fromkeys(keys))


def render_markdown(
    *,
    run_id: str,
    window_days: int,
    cataloged: int,
    facts: list[SessionFacts],
    aggregate: dict[str, Any],
    synthesis: dict[str, Any],
    actions: list[dict[str, Any]],
    prior_actions: list[dict[str, Any]],
    served_model: str,
    omitted: int,
    problems: list[str],
) -> str:
    by_key = {f.key: f for f in facts}
    total = len(facts)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out: list[str] = []
    out.append(f"# Personal Insights: last {window_days} days")
    out.append("")
    out.append(
        f"Generated {now}. {total} of {cataloged} dashboard sessions analyzed. Private to the owner."
    )
    out.append("")
    out.append("## What stands out")
    out.append("")
    out.append(_link_prose(synthesis.get("recognition", "").strip(), by_key))
    out.append("")
    if prior_actions:
        out.append("## Follow-through on earlier actions")
        out.append("")
        for measured in follow_through(prior_actions, facts):
            out.extend(_follow_through_lines(measured))
    out.append("## Recommended next changes")
    out.append("")
    if not actions:
        out.append(
            "No change cleared the evidence bar this run. The effective patterns below are worth "
            "keeping as they are."
        )
        out.append("")
    for action in actions:
        keys = _claim_keys(synthesis, action["claim_ids"])
        out.append(f"### {action['rank']}. {action['title']}")
        out.append("")
        out.append(f"**[{action['cta']}]**  Copy  ·  Not for me")
        out.append("")
        out.append(action["why"].strip())
        out.append("")
        out.append(f"Evidence: {_evidence_line(keys, total, by_key)}")
        out.append("")
        label = {
            "lesson_proposal": "Saved lesson the agent will follow",
            "prompt": "Reusable prompt",
            "steering_patch": "Steering addition",
            "existing_capability": "Existing capability",
        }[action["action_class"]]
        out.append(f"{label}:")
        out.append("")
        out.append("```text")
        out.append(action["display_artifact"])
        out.append("```")
        out.append("")
        out.append(f"Expected later: {action['expected_observation'].strip()}")
        out.append("")
        out.append(f"Verify: {action['verification'].strip()}")
        out.append("")
        out.append(f"Undo: {action['undo'].strip()}")
        out.append("")
        if action["executable"]:
            out.append(
                "Status: proposed. Do it applies this through the lesson store, verifies it by "
                "readback, and offers Undo."
            )
        else:
            out.append("Status: proposed. This class is copy-first in this slice.")
        out.append("")
        out.append("```insights-action")
        out.append(
            json.dumps(
                {
                    k: action[k]
                    for k in (
                        "action_id",
                        "action_key",
                        "action_class",
                        "behavior_predicate",
                        "artifact",
                        "artifact_digest",
                        "cta",
                        "executable",
                    )
                },
                indent=1,
                sort_keys=True,
            )
        )
        out.append("```")
        out.append("")
    out.append("## What is working")
    out.append("")
    for claim in synthesis.get("working", []):
        out.append(f"- {_link_prose(claim['text'].strip(), by_key)}")
        out.append(f"  {_evidence_line(claim.get('session_keys', []), total, by_key)}")
    out.append("")
    out.append("## Where time or quality is lost")
    out.append("")
    for claim in synthesis.get("friction", []):
        out.append(f"- {_link_prose(claim['text'].strip(), by_key)}")
        if claim.get("consequence"):
            out.append(f"  Consequence: {claim['consequence'].strip()}")
        out.append(f"  {_evidence_line(claim.get('session_keys', []), total, by_key)}")
    out.append("")
    out.append("## How you work with the agent")
    out.append("")
    out.append(_link_prose(synthesis.get("interaction_style", "").strip(), by_key))
    out.append("")
    if synthesis.get("horizon"):
        out.append("## On the horizon")
        out.append("")
        for item in synthesis["horizon"]:
            out.append(f"- {item.strip()}")
        out.append("")
    out.append("## Activity")
    out.append("")
    out.append("| Measure | Value |")
    out.append("|---|---|")
    out.append(f"| Sessions analyzed | {aggregate['sessions']} |")
    out.append(f"| Owner messages | {aggregate['owner_turns']} |")
    out.append(f"| Assistant turns | {aggregate['assistant_turns']} |")
    out.append(f"| Tool calls | {aggregate['tool_calls']} |")
    out.append(f"| Tool errors or blocks | {aggregate['tool_errors']} |")
    out.append(f"| Stall recoveries | {aggregate['stalls']} |")
    out.append(f"| Subagent dispatches | {aggregate['subagent_dispatches']} |")
    out.append(
        f"| Sessions with correction markers | {aggregate['sessions_with_corrections']} of {total} |"
    )
    out.append(
        f"| Wall-clock span, first to last message, minutes | {aggregate['active_minutes']} |"
    )
    out.append("")
    out.append(
        "Most used tools: " + ", ".join(f"{n} ({c})" for n, c in aggregate["top_tools"][:10])
    )
    out.append("")
    out.append("### Sessions in this report")
    out.append("")
    out.append("| Session | Started | Minutes | Owner turns | Tools | Errors |")
    out.append("|---|---|---|---|---|---|")
    for f in facts:
        out.append(
            f"| {_session_link(f.key, by_key)} | {f.started[:10]} | {f.duration_minutes} | "
            f"{f.owner_turns} | {f.tool_calls} | {f.tool_errors} |"
        )
    out.append("")
    moment = synthesis.get("memorable_moment")
    if moment and moment.get("text") and moment.get("session_key") in by_key:
        out.append("## A moment worth remembering")
        out.append("")
        out.append(_link_prose(moment["text"].strip(), by_key))
        out.append("")
        out.append(_session_link(moment["session_key"], by_key))
        out.append("")
    out.append("## Coverage and method")
    out.append("")
    out.append(
        f"- Source: dashboard-origin sessions in the last {window_days} days, most recent first, "
        f"capped for this run. {omitted} selected sessions were omitted because facet analysis failed."
    )
    out.append(
        "- Measured facts (counts, durations, tools, correction markers) are deterministic. "
        "Narrative claims are model interpretation and cite the sessions they rest on."
    )
    out.append(
        "- Owner text was minimized before inference: identities, emails, private paths, and "
        "credential-shaped strings were replaced with typed markers. No tool output bodies, "
        "attachments, or subagent transcripts were sent."
    )
    out.append(f"- Inference ran through the owner's own Kiro Crew backend, model: {served_model}.")
    out.append("- Evidence counts are session counts, not probabilities.")
    for note in synthesis.get("coverage_notes", []):
        out.append(f"- {note.strip()}")
    if problems:
        out.append(f"- Validation dropped or corrected {len(problems)} model outputs.")
    out.append("")
    out.append(f"Run `{run_id}`.")
    out.append("")
    return "\n".join(out)
