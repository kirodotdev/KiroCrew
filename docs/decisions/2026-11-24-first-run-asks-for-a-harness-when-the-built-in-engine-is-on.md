# First run asks for a harness, then the Kiro sign-in, when the built-in engine is on

Decided by: Zezhen Xu (@CrysisDeu, owner)
Date: 2026-11-24
Supersedes: 2026-11-13-first-run-offers-the-kiro-sign-in-with-a-skip.md (decided by Bolin Chen), in part: only for first-run loads where the built-in engine is on

## Decision

On a first-run load where the built-in engine is on (`engine_choice: "engine"` on `GET /api/kas-bundle`: the saved `agent.kas_bundle`, else the build's preference), the dashboard shows one setup step before settings import and the tour: choose a harness (Built-in harness first and picked; Kiro CLI always listed, with "Not installed", its install command and a copy button while it is missing, and Continue off while it is picked; the other harnesses behind one "Other harnesses" row that lists what Settings > Agent Harness lists), sign in through the shared Kiro sign-in with the looking ghost centered over its title and the back arrow at the top left, then "You're all set"; the pick is saved with the keys Settings writes and asks for no restart; a harness that signs in on its own gets one line plus "Open Settings"; "Set up later" saves nothing and lets the app render for that page load, and the tour's last card then says "Almost ready"; with the built-in engine off, the 2026-11-13 entry stands unchanged.

## Why

- The full-screen sign-in alone gave a new person no way to pick Kiro CLI or another harness on first run; the choice lived only in Settings > Agent Harness.
- The step shows only where the built-in engine is on, so a source checkout or `--no-kas` keeps main's first run.
- The 2026-11-13 entry was Bolin Chen's, and it stays in force for first-run loads where the built-in engine is off.
- The owner picked each prototype round from v1 to v5 and closed with "先这样吧，babysit到绿" (2026-11-24 21:40 UTC).

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/42203#issuecomment-7147616891 -- the owner's restated ruling.
- https://github.com/kirodotdev/KiroCrew/issues/42168 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/42203 -- the change.
