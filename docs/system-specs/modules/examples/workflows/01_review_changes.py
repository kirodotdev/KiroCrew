"""Example dynamic workflow: review a diff across dimensions, verify each finding.

This is the canonical pipeline pattern, ported from an external agent CLI's
`Workflow` tool into the proposed Kiro Crew Python DSL. Illustrative only — the
DSL is not implemented yet.

Run:  workflow run review-changes --args '{"cr": "CR-1234567"}'
"""

# META must be a pure literal (no calls, no interpolation) — parsed statically.
META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

# Structured-output schemas are pydantic-style dicts (JSON Schema under the hood).
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "file", "detail"],
            },
        }
    },
    "required": ["findings"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_real": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_real", "reason"],
}

DIMENSIONS = [
    {"key": "bugs", "prompt": "Review the diff for correctness bugs."},
    {"key": "security", "prompt": "Review the diff for security issues."},
    {"key": "perf", "prompt": "Review the diff for performance regressions."},
]


async def workflow(ctx):
    cr = ctx.args["cr"]
    ctx.log(f"Reviewing {cr} across {len(DIMENSIONS)} dimensions")

    # pipeline(): each dimension's findings get verified the moment that
    # dimension's review finishes — no barrier between Review and Verify.
    async def review_stage(dim):
        return await ctx.agent(
            f"{dim['prompt']} CR={cr}",
            label=f"review:{dim['key']}",
            phase="Review",
            schema=FINDINGS_SCHEMA,
        )

    async def verify_stage(review, dim, _index):
        async def verify_one(f):
            verdict = await ctx.agent(
                f"Adversarially verify this {dim['key']} finding — try to refute it:\n{f}",
                label=f"verify:{f['file']}",
                phase="Verify",
                schema=VERDICT_SCHEMA,
            )
            return {**f, "verdict": verdict}

        return await ctx.parallel([lambda f=f: verify_one(f) for f in review["findings"]])

    results = await ctx.pipeline(DIMENSIONS, review_stage, verify_stage)

    confirmed = [
        f for group in results if group
        for f in group if f and f["verdict"] and f["verdict"]["is_real"]
    ]
    ctx.log(f"{len(confirmed)} confirmed findings")
    return {"cr": cr, "confirmed": confirmed}
