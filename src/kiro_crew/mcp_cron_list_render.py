"""How ``cron_list`` renders the scheduled jobs: full, compact and JSON.

The preview caps, the next-run suffix and text sanitizing, the JSON mode's byte and
job budgets, and the run-history fold it reads from the gateway (no-op summaries
folded into a count).

Served through :mod:`kiro_crew.mcp_cron`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import datetime
from typing import Any, Iterable

from kiro_crew.config.loader import read_local_secret
from kiro_crew.cron import compute_next_run_ts, format_schedule, get_local_tz
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.port_resolution import resolve_serving_port

logger = logging.getLogger("kiro_crew.mcp_cron")


# ── cron_list rendering ──

# Compact-mode caps. Tuned so 50 jobs stay well under 30KB.
_MSG_PREVIEW_LEN = 80
_ERR_PREVIEW_LEN = 200
_RESULT_PREVIEW_LEN = 120


def _format_next_run(job: Any, now: float, local_tz: Any) -> str:
    """Format the 'Next run' suffix for a job, or empty string if no next run."""
    nxt = compute_next_run_ts(job, now=now)
    if nxt is None:
        return ""
    delta = nxt - now
    if delta >= 86400:
        d = int(delta // 86400)
        h = int((delta % 86400) // 3600)
        rel = f"in {d}d {h}h"
    elif delta >= 3600:
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        rel = f"in {h}h {m}m"
    elif delta > 0:
        m = int(delta // 60)
        rel = f"in {m}m" if m >= 1 else "in <1m"
    else:
        rel = "now"
    # A representable extreme (a beyond-year-9999 stamp, a pre-epoch value
    # on Windows) must degrade to a fallback string instead of raising
    # inside the loop that renders EVERY job in cron_list -- the same
    # degrade-on-render posture as format_schedule and the CLI formatter.
    try:
        local_str = datetime.fromtimestamp(nxt, tz=local_tz).strftime("%Y-%m-%d %I:%M %p %Z")
    except Exception:
        logger.debug("_format_next_run: unrenderable next-run ts %r", nxt, exc_info=True)
        return "\n  Next run: at an invalid stored time"
    return f"\n  Next run: {local_str} ({rel})"


def _sanitize(s: str) -> str:
    """Redact credentials and exfiltration URLs from a string.

    Routes through the context-aware ``redact`` shim so a loaded companion's
    extra regexes apply; standalone is byte-for-byte today's two-pass.
    """
    return redact(s)


# ── cron_list JSON mode ──
#
# A skill script cannot read the job store or the run history itself. The store is
# only sandbox-visible because ``mcp_cron`` was carved out for it
# (``sandbox._CREW_SANDBOX_VISIBLE_LEAVES``), and reading it directly bypasses the
# ownership filter every MCP cron tool applies -- a non-owner sharing one data home
# would see every participant's job metadata. ``cron-history`` is masked outright,
# and on Linux the mask is an empty writable directory, so a direct read reports
# zero runs for every job and looks identical to a job that has never fired.
#
# So both reads come through here: ownership is decided from the gateway-vouched
# session key, and history is fetched from the gateway for the ALREADY-SCOPED ids
# only. A history read that cannot happen is reported as unavailable, never as zero.

#: Recent runs asked of the gateway per job. Matches the audit window the
#: cron-cost-optimize skill reasons over.
_JSON_HISTORY_LIMIT = 40

#: Jobs whose history is fetched in one call. An upper bound on work, not on
#: payload size -- the size bound is ``_JSON_BYTE_BUDGET``, because a count cannot
#: bound bytes.
_JSON_MAX_JOBS = 100

#: Serialized characters the records may occupy. A COUNT cap does not bound size:
#: ``json.dumps`` escapes a non-ASCII character to ``\uXXXX``, six characters for
#: one, so 100 ordinary 400-character prompts in Chinese serialize to ~296,000
#: characters -- almost three times the response ceiling. Measured, not estimated.
#: Crossing that ceiling matters more than losing a row, because
#: ``sanitize_response`` truncates with a blind tail slice that appends a notice
#: OUTSIDE the JSON grammar, so the consumer gets a document that does not parse
#: at all rather than a short one. Held below ``MAX_RESPONSE_LEN`` with room for
#: the envelope and the unavailable-reason string.
_JSON_BYTE_BUDGET = 88_000

#: Prompt text carried per job. Longer than the compact preview, because a
#: consumer classifies the prompt rather than displaying it, and shorter than the
#: full body, which no classifier needs and which would blow the payload budget.
_JSON_MESSAGE_LEN = 400

#: Per-request and whole-phase ceilings for the history fetch. A slow or absent
#: gateway degrades the payload, it never hangs the tool.
_JSON_HISTORY_TIMEOUT_SECS = 3.0
_JSON_HISTORY_BUDGET_SECS = 20.0


#: Result text that means a run found nothing to do. Matched against a run's own
#: summary, so it describes what the job SAID, not what its prompt asked for.
_NOOP_RE = re.compile(
    r"("
    r"nothing to do|nothing new|nothing to report|nothing changed|"
    r"no new |no change|no changes|unchanged|no update|no action|"
    r"none found|no matches|no results|no failures|no errors|no issues|"
    r"all clear|all good|all healthy|clean run|looks healthy|is healthy|"
    r"up to date|already (done|handled|posted|processed|triaged)|"
    r"skipped|no-op|idle|0 found|0 new|zero new"
    r")",
    re.IGNORECASE,
)


def _normalize_summary(text: str) -> str:
    """Collapse a run summary so two runs that said the same thing compare equal.

    Digits become ``#`` because a timestamp, a count or a percentage changing is
    exactly the case that reads as different while meaning the same thing: "tmp
    1%, home 19%" and "tmp 4%, home 22%" are one result, not two.
    """
    lowered = re.sub(r"\d+", "#", text.strip().lower())
    return re.sub(r"\s+", " ", lowered)


def _fold_runs(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a job's run records to the counts a cost audit needs.

    ``cron-history`` is bind-masked from every sandboxed agent shell
    (``sandbox._CREW_HIDDEN_LEAVES``), so a skill script cannot read run history
    itself; it has to come from the gateway. What crosses that boundary should be
    COUNTS rather than the run text they came from -- a count cannot carry a
    credential, and folding here keeps a many-job payload well under the ceiling.

    ``records`` are history rows as the store serializes them, so each carries at
    least ``status`` and ``summary``. A row whose status is present and not
    ``success`` counts as a failure and contributes to no other tally: a run that
    crashed says nothing about whether the job had work to do.

    ``same_every_run`` needs more than one run to mean anything, so a single
    recorded run reports False rather than trivially True.
    """
    runs = 0
    failures = 0
    noop_runs = 0
    seen: set[str] = set()
    for rec in records:
        status = str(rec.get("status") or "")
        if status and status != "success":
            failures += 1
            continue
        runs += 1
        summary = str(rec.get("summary") or "")
        seen.add(_normalize_summary(summary))
        if _NOOP_RE.search(summary):
            noop_runs += 1
    return {
        "runs": runs,
        "failures": failures,
        "distinct_summaries": len(seen),
        "noop_runs": noop_runs,
        "same_every_run": runs > 1 and len(seen) == 1,
    }


def _fetch_history_stats(job_ids: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """Ask the gateway to fold each job's run history into counts.

    Returns ``(stats_by_job_id, unavailable_reason)``. An empty reason means every
    id in *job_ids* was fetched. A non-empty reason means some or all could not be,
    and the caller MUST surface it rather than let a missing entry read as a job
    that has never run.

    Loopback plus ``X-Internal-Secret``, the same path ``cron_trigger`` uses, with
    the same port and credential resolvers -- the serving port rather than the
    configured one, and the per-listener secret ahead of the home-wide fallback.
    """
    if not job_ids:
        return {}, ""
    try:
        port = resolve_serving_port()
    except Exception as exc:  # pragma: no cover - resolver is defensive already
        return {}, f"cannot resolve the gateway port ({type(exc).__name__})"
    secret = read_local_secret(port, dial_host="127.0.0.1")
    if not secret:
        return {}, "no gateway credential on this host, so run history cannot be read"

    stats: dict[str, dict[str, Any]] = {}
    started = time.time()
    for jid in job_ids:
        if time.time() - started > _JSON_HISTORY_BUDGET_SECS:
            return stats, "the run-history fetch ran out of time before every job was read"
        url = f"http://127.0.0.1:{port}/api/crons/{jid}/history?limit={_JSON_HISTORY_LIMIT}"
        req = urllib.request.Request(url, method="GET", headers={"X-Internal-Secret": secret})
        try:
            with loopback_urlopen(req, timeout=_JSON_HISTORY_TIMEOUT_SECS) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            # One unreachable gateway means none of the rest will answer either.
            return stats, f"the gateway did not answer the run-history read ({type(exc).__name__})"
        runs = body.get("runs")
        stats[jid] = _fold_runs(runs if isinstance(runs, list) else [])
    return stats, ""


def _render_cron_list_json(jobs: list[Any]) -> str:
    """Ownership-scoped job records plus folded run-history counts, as JSON.

    Every free-text field is sanitized BEFORE it is truncated. The other order
    leaves a credential's prefix in the surviving span, which is why the compact
    renderer has a test named for it.

    The payload bounds itself and says so. Relying on the response-level cap would
    hand a consumer a blind tail slice of a JSON document, which does not parse.
    """
    kept = jobs[:_JSON_MAX_JOBS]
    stats, unavailable = _fetch_history_stats([j.id for j in kept])

    records: list[dict[str, Any]] = []
    used = 0
    dropped = False
    for job in kept:
        history = stats.get(job.id)
        message = _sanitize(job.message or "")
        record = {
            "id": job.id,
            "name": _sanitize(job.name or "")[:_MSG_PREVIEW_LEN],
            "mode": _job_kind(job),
            "enabled": bool(getattr(job, "enabled", True)),
            "schedule": format_schedule(job.schedule, tz_name=job.timezone or ""),
            "every_secs": getattr(job.schedule, "every_secs", None),
            "minimal_context": bool(job.minimal_context),
            "persistent_session": bool(job.persistent_session),
            "hide_in_chat": bool(job.hide_in_chat),
            "message": message[:_JSON_MESSAGE_LEN],
            # A consumer classifies the prompt, and anything past the cut is
            # invisible to it -- including the words that would RULE OUT a
            # cheaper mode. Saying the text was cut is what lets it refuse to
            # judge instead of judging on half a prompt.
            "message_truncated": len(message) > _JSON_MESSAGE_LEN,
            # None, never an empty tally: a job whose history could not be read
            # has not been shown to be idle, and a consumer must be able to
            # tell those two apart.
            "history": history,
        }
        # Measure what this record actually costs SERIALIZED, then decide. The
        # alternative -- assemble everything and check at the end -- has no way
        # to shed a row without re-serializing, and guessing a per-record size
        # is what the count cap already got wrong.
        cost = len(json.dumps(record, indent=2, sort_keys=True)) + 4
        if records and used + cost > _JSON_BYTE_BUDGET:
            dropped = True
            break
        records.append(record)
        used += cost

    payload: dict[str, Any] = {
        "scanned": len(records),
        # True when ANY owned job is missing from this payload, whichever bound
        # dropped it. A consumer only needs to know the scan is partial.
        "truncated": dropped or len(jobs) > len(kept),
        "history_available": not unavailable,
        "jobs": records,
    }
    if unavailable:
        payload["history_unavailable_reason"] = unavailable
    return json.dumps(payload, indent=2, sort_keys=True)


def _job_kind(job: Any) -> str:
    """Return 'script' / 'command' / 'agent' for a cron job."""
    if getattr(job, "script", ""):
        return "script"
    if getattr(job, "command", ""):
        return "command"
    return "agent"


def _render_cron_list_full(jobs: list[Any]) -> str:
    """Legacy (verbose) cron_list output — full message body per job.

    This rendering MUST stay byte-for-byte stable so that ``verbose=true``
    keeps working for existing callers that parse this format.
    """
    active = sum(1 for j in jobs if j.enabled)
    paused = len(jobs) - active
    header = f"{len(jobs)} cron job(s): {active} active, {paused} paused\n"
    lines: list[str] = [header]
    now = time.time()
    tz_name, local_tz = get_local_tz()
    for j in jobs:
        status = "✅ active" if j.enabled else "⏸️ paused"
        sched = format_schedule(j.schedule, tz_name=j.timezone or tz_name)
        next_line = _sanitize(_format_next_run(j, now, local_tz))
        san_name = _sanitize(j.name)
        san_msg = _sanitize(j.message)
        san_sched = _sanitize(sched)
        kind = _job_kind(j)
        entry = f"• {san_name} ({status}) [{kind}]\n  ID: {j.id} | {san_sched}{next_line}\n  → {san_msg}"
        if j.last_status == "error" and j.last_error:
            san_err = _sanitize(j.last_error)
            entry += f"\n  ⚠️ last error: {san_err}"
        elif (
            j.last_status == "ok"
            and (j.script or j.command)
            and j.last_result
            # Registries written before result_produced existed persist a literal
            # "ok" sentinel, which would render as though it were real output.
            and j.last_result != "ok"
        ):
            san_res = _sanitize(j.last_result)[:_RESULT_PREVIEW_LEN]
            entry += f"\n  last result: {san_res}"
        lines.append(entry)
    return "\n".join(lines)


def _render_cron_list_compact(jobs: list[Any]) -> str:
    """Compact cron_list output — one short summary block per job.

    Drops full message bodies in favour of an 80-char preview, and
    truncates last_error / last_result to bounded sizes. Adds kind /
    agent / channel / last-status signal that the legacy format omitted
    or only included on certain branches. Sized so a 50-job registry
    stays well under 30KB.

    Sanitize-then-truncate ordering is enforced for every
    user-controlled field so a credential straddling the truncation
    boundary cannot leak as a partial fragment.

    Callers that need full bodies pass ``verbose=true`` (legacy shape)
    or ``ids=["<job_id>", ...]`` (drill-in for specific jobs).
    """
    active = sum(1 for j in jobs if j.enabled)
    paused = len(jobs) - active
    # Header intentionally bare — programmatic parsers lock on the regex
    # ``^\d+ cron job\(s\): \d+ active, \d+ paused$``. The "compact mode"
    # hint and the ``verbose=true`` / ``ids=[...]`` opt-outs are documented
    # on the cron_list tool description instead, where MCP clients see them.
    header = f"{len(jobs)} cron job(s): {active} active, {paused} paused\n"
    lines: list[str] = [header]
    now = time.time()
    tz_name, local_tz = get_local_tz()
    for j in jobs:
        status = "✅" if j.enabled else "⏸️"
        sched = format_schedule(j.schedule, tz_name=j.timezone or tz_name)
        next_line = _sanitize(_format_next_run(j, now, local_tz))
        san_name = _sanitize(j.name)
        san_sched = _sanitize(sched)
        kind = _job_kind(j)
        # Sanitize-then-truncate: truncating first could split a credential
        # or exfiltration URL across the boundary and bypass redaction.
        # Newlines also collapsed so each job stays a single block.
        msg_raw = j.message if isinstance(j.message, str) else ""
        msg = msg_raw.strip().replace("\n", " ")
        san_msg_full = _sanitize(msg) if msg else ""
        if len(san_msg_full) > _MSG_PREVIEW_LEN:
            san_msg_preview = san_msg_full[:_MSG_PREVIEW_LEN] + "…"
        else:
            san_msg_preview = san_msg_full
        # Optional signal lines — only emit when present, to keep small jobs short.
        extras: list[str] = []
        agent_raw = getattr(j, "agent_id", "")
        agent = agent_raw.strip() if isinstance(agent_raw, str) else ""
        channel_raw = getattr(j, "channel", None)
        channel = channel_raw.strip() if isinstance(channel_raw, str) else ""
        if agent:
            extras.append(f"agent={_sanitize(agent)}")
        model_raw = getattr(j, "model", "")
        model_val = model_raw.strip() if isinstance(model_raw, str) else ""
        if model_val:
            # _sanitize applies the full redact_credentials +
            # redact_exfiltration_urls chain required for LLM-controlled values.
            extras.append(f"model={_sanitize(model_val)}")
        if channel:
            extras.append(f"channel={_sanitize(channel)}")
        last_status = getattr(j, "last_status", None)
        if isinstance(last_status, str) and last_status:
            extras.append(f"last={_sanitize(last_status)}")
        # Show last_error preview when in error; otherwise last_result for
        # script/command jobs whose result isn't the trivial "ok".
        if last_status == "error" and isinstance(j.last_error, str) and j.last_error:
            san_err = _sanitize(j.last_error)
            err_short = (
                san_err if len(san_err) <= _ERR_PREVIEW_LEN else san_err[:_ERR_PREVIEW_LEN] + "…"
            )
            extras.append(f"err={err_short}")
        elif (
            last_status == "ok"
            and (getattr(j, "script", "") or getattr(j, "command", ""))
            and isinstance(j.last_result, str)
            and j.last_result
            and j.last_result != "ok"
        ):
            san_res = _sanitize(j.last_result)
            res_short = (
                san_res
                if len(san_res) <= _RESULT_PREVIEW_LEN
                else san_res[:_RESULT_PREVIEW_LEN] + "…"
            )
            extras.append(f"result={res_short}")
        extras_line = f"\n  {' | '.join(extras)}" if extras else ""
        msg_line = f"\n  → {san_msg_preview}" if san_msg_preview else ""
        lines.append(
            f"• {san_name} {status} [{kind}]\n  ID: {j.id} | {san_sched}"
            f"{next_line}{extras_line}{msg_line}"
        )
    return "\n".join(lines)
