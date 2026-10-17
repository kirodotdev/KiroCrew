# Fallback local-review charters

Use these ONLY when `local_review.py` exits 40 (a parity failure: a reviewer
workflow no longer has the shape the extractor reads). They are hand-written
paraphrases of CI's contracts and may have drifted; the extracted brief is the
default. Say so with the WARNING line from SKILL.md's Phase 2, and fix the
extractor.

- **`gpt`** — read the `SEVERITY + BLOCKING CONTRACT` / `OUTPUT STYLE` sections of `.github/workflows/codex-review.yml`. Charter: reachable correctness/security failures, data loss, crashes/hangs, permission-boundary regressions, cross-OS breakage, with a **report-ALL** budget — every qualifying finding in one review, never staged across rounds.
- **`opus`** — read `.github/workflows/claude-review.yml` **and, decisively, the BASE-ref `AUTOSDE.yaml` + `website/AUTOSDE.yaml`**, plus `AGENTS.md`. Every finding completes a consequence chain (cause → mechanism → consequence) or is dropped. **BLOCK only on** a `blocking: true` AUTOSDE rule matching a changed file, a reachable security hole, a crash/data-loss/corruption bug, a removed guard with no replacement, or unconditional wrong behaviour on the normal path. Budget: **≤5 BLOCKING, ≤6 advisory FINDING**.
