# The glass material comes in five named thicknesses, chosen per surface, never as raw optics

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-02

## Decision

The Liquid Glass material (`website/src/components/Glass.tsx`) has exactly five thicknesses — `ultrathin`, `thin`, `regular`, `thick`, `ultrathick` — each a fixed pair of backdrop blur and tint (blur 2 / 4 / 8 / 14 / 28 px; tint alpha .30 / .40 / .50 / .60 / .78 dark, .35 / .45 / .55 / .65 / .82 light). A call site picks a thickness and never a blur or an alpha of its own. `thin` is the recipe every pane wore before the ladder existed and stays the default. The tint colour of each step is set so a pane over the plain page keeps one overall colour across the ladder (light: `#f8f8f8` over white), moving toward the page colour as the alpha rises. The progress panes above the composer (sub-agent tray, task bar, workflow bar, Command Center card) wear `thick`; the composer keeps `thin`.

## Why

- Once the transcript scrolls under the composer glass by design (`2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md`), legibility of what floats over it is the material's job, and one blur level cannot serve both a 30px chip and a dense progress pane with a paragraph passing under it.
- The maintainer reviewed three rendered scale options (3 steps, 4 steps, 3 alpha-heavy steps) and chose a five-step ladder: today's recipe kept unchanged as `thin`, one step added below it, the rendered "regular" of the 4-step option read as `thick`, and one step added above. He then reviewed the rendered five-step ladder, accepted that `thick` and `ultrathick` read close (ultrathick being the near-opaque end), and fixed the values.
- The whitening rule came from the maintainer: "在低透明度的时候，用的颜色也要更白，保持在白色底下可以维持 f8f8f8 的总体颜色".
- Steps that no surface wears yet are part of the scale on purpose: the ladder is the material's vocabulary, so a later surface picks a name instead of inventing a number.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/16299 — the request and the maintainer's direction (comments 5962954713 and 5963142485 fix the five steps and their values).
- https://github.com/kirodotdev/KiroCrew/pull/16351#issuecomment-5964651450 — the maintainer's on-record restatement of the decision, on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/16351 — the implementation and the pull request that adds this entry.
