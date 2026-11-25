# `pr_status.py` exit 20: triage before re-pushing

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core (Phase 3 step 5).

## Phase 3 step 5: the green-age check

   `python3 $SKILL_DIR/scripts/green_age.py --pr <n>` first. **0** → nothing to do.
   **2** → keep polling, and after **three consecutive** 2s report the reason and
   hand the PR over. **30** → re-check in a throwaway detached worktree
   (`rationale.md`). **Green: do NOT push**; on a red or conflict, fix it, push with the SHA-pinned lease, and post ONE
   comment: `Rebased: main moved in <files> since this head went green (<old-base> -> <new-base>); CI re-running.`
   Exit 30 does NOT override the no-push-while-runs-are-in-flight rule above.

## Phase 3 step 5: exit 20

   - **20** → run `pr_findings.py` and **TRIAGE before re-pushing**. `pr_status.py` already reads only the current head's rollup and drops rows a newer run of the same check displaced; never dedupe check rows by name by hand, which drops live rows. **(d) Superseded or unpublished verdict** (`superseded verdict`, `verdict not published, re-run this lane`) → re-run that lane, or override at this head. **(a) CI/build/test failure** → read `gh run view <run-id> --log-failed`. If the same check also fails on main, follow *When main is red*. Otherwise, once you decide to fix, cancel the head's in-flight runs, reproduce the **exact failing node ids** locally, and fix the **root cause**; a flake confirmed and recorded per *Before you rerun a red test* gets `gh run rerun <run-id> --failed` (or `--job <job-id>`) once, never a whole-run replay. **(b) Review finding** → whole-design verdicts first, then the three questions. For Kiro Crew Opus-family or GPT 6.1 findings that need code changes, MUST execute [Review repair routing](../SKILL.md#review-repair-routing): delegate the minimal fix and self-review to the model-pinned subagent, then verify in the parent. Otherwise rebut with evidence (never dismiss a CodeQL alert merely to pass), override it per *Overriding a false positive*, or ask a maintainer. **(c) Conflict / behind base** → Phase 1 handles it. Then **loop back to Phase 1** → 2 → 3 carrying those fixes.
