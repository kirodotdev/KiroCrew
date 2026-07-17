"""Example dynamic workflow: KiroCrew-native scheduled morning triage.

Shows the primitives that make KiroCrew workflows MORE than Claude Code
workflows — persistence, cron self-scheduling, memory-aware behavior, nudges,
and Slack delivery. Illustrative only — DSL not implemented yet.

Author once; it re-arms itself daily and learns which finders are noisy.
"""

META = {
    "name": "ticket-triage",
    "description": "Daily: fan out over open tickets, judge, post a digest, self-reschedule",
    "phases": [{"title": "Fetch"}, {"title": "Triage"}, {"title": "Deliver"}],
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "priority": {"type": "string", "enum": ["P1", "P2", "P3", "noise"]},
        "summary": {"type": "string"},
        "suggested_action": {"type": "string"},
    },
    "required": ["priority", "summary"],
}


async def workflow(ctx):
    # --- memory-aware: down-weight finders we learned are noisy ---
    noisy = ctx.memory.get("triage.noisy_resolvers", default=[])
    ctx.log(f"Skipping {len(noisy)} resolver groups learned as noisy")

    # --- ensure we run again tomorrow (idempotent) ---
    ctx.cron.ensure("ticket-triage-daily", cron_expr="0 9 * * *",
                    workflow="ticket-triage", timezone="US/Pacific")

    ctx.phase("Fetch")
    tickets = await ctx.agent(
        "List my open resolver-group tickets as JSON.",
        schema={"type": "object",
                "properties": {"tickets": {"type": "array", "items": {"type": "object"}}},
                "required": ["tickets"]},
    )
    queue = [t for t in tickets["tickets"] if t.get("resolver") not in noisy]

    # --- triage each ticket in parallel; nudge any step that stalls ---
    ctx.phase("Triage")
    async def triage_one(t):
        return await ctx.agent(
            f"Triage ticket {t['id']}: {t.get('title', '')}",
            label=f"triage:{t['id']}",
            schema=TRIAGE_SCHEMA,
            # if this step idles >120s, self-prompt to summarize-and-continue
            nudge={"idle_secs": 120, "message": "Still working? Summarize progress and finish."},
        )

    triaged = await ctx.parallel([lambda t=t: triage_one(t) for t in queue])
    actionable = [r for r in triaged if r and r["priority"] in ("P1", "P2")]

    # --- learn: resolver groups that produced only noise get down-weighted ---
    noise_ratio = _noise_by_resolver(queue, triaged)
    ctx.memory.set("triage.noisy_resolvers",
                   sorted({r for r, ratio in noise_ratio.items() if ratio > 0.9}))

    # --- deliver a digest to the owner's Slack DM ---
    ctx.phase("Deliver")
    digest = await ctx.agent(f"Write a concise morning triage digest from: {actionable}")
    await ctx.send_slack(ctx.owner_dm, digest)

    return {"triaged": len(triaged), "actionable": len(actionable)}


def _noise_by_resolver(queue, triaged):
    by = {}
    for t, r in zip(queue, triaged):
        rg = t.get("resolver", "unknown")
        is_noise = bool(r) and r["priority"] == "noise"
        n, d = by.get(rg, (0, 0))
        by[rg] = (n + (1 if is_noise else 0), d + 1)
    return {rg: n / d for rg, (n, d) in by.items() if d}
