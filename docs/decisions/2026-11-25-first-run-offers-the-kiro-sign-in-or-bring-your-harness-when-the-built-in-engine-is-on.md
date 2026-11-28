# First run offers the Kiro sign-in or "Bring your harness" when the built-in engine is on

Decided by: Zezhen Xu (@CrysisDeu, owner)
Date: 2026-11-25
Supersedes: 2026-11-24-first-run-asks-for-a-harness-when-the-built-in-engine-is-on.md (decided by Zezhen Xu), for first-run loads where the built-in engine is on

## Decision

On a first-run load where the built-in engine is on, the setup step opens on the looking ghost and "How do you want to start?" centered over the shared Kiro sign-in choices, inline; a sign-in that lands saves the built-in harness and shows "You're all set" (with a sign-in already stored, one "Continue with Kiro" does the same); with Kiro CLI installed and signed in, "Continue with Kiro CLI" keeps it in one click; below them, "Bring your harness" (this label) shows one line naming what only a Kiro sign-in gives, then one list with Kiro CLI first (its install command while missing) and every other harness Settings > Agent Harness lists, with a back arrow; there is no Built-in harness radio and no "Other harnesses" row; "You're all set" centers the ghost under one burst of confetti that plays once, throws the same pieces every time, uses theme colours only and shows nothing under reduced motion; "Set up later", the hold, the save keys and the engine-off first run stay as the 2026-11-24 entry and the 2026-11-13 entry say.

## Why

- Kyle proposed two choices only, "Sign in to Kiro" and "Bring your harness", and the owner asked that skipping the sign-in say what you miss before the harness list.
- The owner then asked for the sign-in choices on the first screen, for one click to keep a signed-in Kiro CLI, and for success idea 8, "Confetti once".
- The owner adopted the experiment with "we can publish https://github.com/kirodotdev/KiroCrew/pull/42767 and babysit to green" (2026-11-25).

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/42767#issuecomment-7178644968 -- the owner's restated ruling.
- https://github.com/kirodotdev/KiroCrew/issues/42747 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/42767 -- the change.
