# Fallback local-review charters

Use these ONLY when `local_review.py` exits 40 (a parity failure: a reviewer
workflow no longer has the shape the extractor reads). They are hand-written
paraphrases of CI's contracts and may have drifted; the extracted brief is the
default. Say so with the WARNING line from SKILL.md's Phase 2, and fix the
extractor. Not when the failure says the running copy is not the base commit's
copy: run the base copy it names instead.

- **`gpt`** — read the `SEVERITY + BLOCKING CONTRACT` / `OUTPUT STYLE` sections of `.github/workflows/codex-review.yml`. Charter: reachable correctness/security failures, data loss, crashes/hangs, permission-boundary regressions, cross-OS breakage, with a **report-ALL** budget — every qualifying finding in one review, never staged across rounds.
- **`opus`** — read `.github/workflows/claude-review.yml` **and, decisively, the BASE-ref `AUTOSDE.yaml` + `website/AUTOSDE.yaml`**, plus `AGENTS.md`. Every finding completes a consequence chain (cause → mechanism → consequence) or is dropped. **BLOCK on** a `blocking: true` AUTOSDE rule matching a changed file, or any defect with a stated trigger and an observable wrong outcome (a security hole, a crash/data-loss/corruption bug, a removed guard with no replacement, wrong behaviour on the normal path). Budget: **≤5 BLOCKING**, each with a `Trigger:` and an `Outcome:`.
