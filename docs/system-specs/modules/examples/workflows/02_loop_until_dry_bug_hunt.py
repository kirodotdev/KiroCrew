"""Example dynamic workflow: exhaustive bug hunt (loop-until-dry + budget).

Demonstrates two relevant to Kiro Crew patterns at once:
  - loop-until-dry: keep fanning out finders until K consecutive clean rounds
  - loop-until-budget: stop early if the token budget runs low

Dedup is against EVERYTHING seen (not just confirmed) so judge-rejected bugs
don't reappear every round. Illustrative only — DSL not implemented yet.
"""

META = {
    "name": "exhaustive-bug-hunt",
    "description": "Find bugs until rounds go dry or the token budget is exhausted",
    "phases": [{"title": "Find"}, {"title": "Judge"}],
}

BUGS_SCHEMA = {
    "type": "object",
    "properties": {
        "bugs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "desc": {"type": "string"},
                },
                "required": ["file", "desc"],
            },
        }
    },
    "required": ["bugs"],
}

VERDICT = {
    "type": "object",
    "properties": {"real": {"type": "boolean"}},
    "required": ["real"],
}

FINDERS = [
    {"name": "logic", "prompt": "Hunt for logic/correctness bugs."},
    {"name": "edge", "prompt": "Hunt for edge-case and boundary bugs."},
    {"name": "concurrency", "prompt": "Hunt for race/async bugs."},
]


def _key(bug):
    return f"{bug['file']}:{bug.get('line', 0)}:{bug['desc'][:40]}"


async def workflow(ctx):
    seen, confirmed, dry = set(), [], 0
    max_dry = ctx.args.get("max_dry_rounds", 2)

    while dry < max_dry:
        # budget is a HARD ceiling; remaining() is inf if no target was set.
        if ctx.budget.total and ctx.budget.remaining() < 50_000:
            ctx.log("Budget nearly exhausted — stopping early (NOT a full sweep)")
            break

        ctx.phase("Find")
        rounds = await ctx.parallel(
            [lambda f=f: ctx.agent(f["prompt"], label=f"find:{f['name']}", schema=BUGS_SCHEMA)
             for f in FINDERS]
        )
        fresh = [b for r in rounds if r for b in r["bugs"] if _key(b) not in seen]
        if not fresh:
            dry += 1
            ctx.log(f"Dry round {dry}/{max_dry}")
            continue

        dry = 0
        for b in fresh:
            seen.add(_key(b))

        ctx.phase("Judge")
        judged = await ctx.parallel(
            [lambda b=b: ctx.agent(f"Is this a real bug? {b['desc']}", schema=VERDICT)
             .then(lambda v, b=b: (b, v)) for b in fresh]
        )
        confirmed += [b for (b, v) in judged if v and v["real"]]
        ctx.log(f"{len(confirmed)} confirmed; {ctx.budget.remaining() // 1000}k tokens left")

    return {"confirmed": confirmed, "total_seen": len(seen), "dry_rounds": dry}
