# The project profile

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core.

Setup, gates, reviewers and conventions come from a resolved profile, not from this
prose. Resolve once per run and keep the JSON for Phases 1–3:

```bash
python3 $SKILL_DIR/scripts/resolve_profile.py > /tmp/pp-profile.json
```

Its resolution order and the keys the JSON always carries: `references/rationale.md`.

**Every profile input is read from the base ref, not the checkout** — otherwise a
branch could drop the lane that reviews it. A ref resolving to nothing is a hard
error (exit 2). So an **uncommitted `.prepare-pr.toml` edit is ignored**.

- **In Kiro Crew:** the bundled profile supplies Playwright setup, the complete
  gate floor, the CI-mirroring `gpt` and `opus` local reviewers,
  `single_commit = true`, and readiness context `PR Readiness`. Read the model
  IDs from the resolved profile; repair-family preferences above never replace
  those read-only local reviewer selections.
- **Elsewhere:** auto-detected gates + reviewers, or whatever `.prepare-pr.toml` declares. Pass a non-default readiness name via `--readiness-context` or `PREPARE_PR_READINESS_CONTEXT`; with none, `pr_status.py` uses the full rollup.

**`single_commit` governs history handling in one place.** When `true`, run the
squash (Phase 1.4) and the post-squash guard (Phase 3.1); when `false`, skip both
and keep the branch's history. PRs are squash-merged by default; CI does not count
commits; the squash makes each push one reviewed commit.

Design + `.prepare-pr.toml` schema: `docs/request-for-change/rfc-prepare-pr-portability.md`.
