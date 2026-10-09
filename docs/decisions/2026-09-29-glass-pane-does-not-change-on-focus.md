# A Liquid Glass pane does not change on focus; the caret is the only focus indicator on a glass input

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-09-29

## Decision

A Liquid Glass pane (`.glass-shadow`) renders identically whether or not a control inside it has focus: no accent ring, no brighter tint, no darker side lines, no deeper shadow, in glass and solid modes alike. On a glass input (the composer textarea, the search filter bar, the mobile Settings capsule) the caret is the only focus indicator. A glass button keeps the app's own `:focus-visible` ring, and a boxed input outside the glass (the desktop Settings search bar) keeps its neutral focus ring.

## Why

- https://github.com/kirodotdev/KiroCrew/pull/14568 shipped the glass composer dock with an accent glow and ring on focus and a brighter focused tint. The maintainer asked for the accent to go ("输入框聚焦去 accent", "setting的bar聚焦的时候也去掉accent") and #14887 removed it but kept a neutral step (darker side lines, deeper shadow). He then asked for no difference at all ("两边都改成完全无区别") and #14992 removed the remaining focus rules and the `--glass-edge-focus` token.
- The design review lane asked for a decision entry at the time; the maintainer declined it as out of that PR's frozen scope, and a second maintainer (@iamwhatever) asked that a maintainer record the accessibility trade-off explicitly. The maintainer's restatement on the pull request that adds this entry is that record. `ChatInput.liquidGlass.test.tsx` pins the absence of the focus tokens.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/14992#issuecomment-5889441859 -- the maintainer's on-record statement on the pull request that finished the change.
- https://github.com/kirodotdev/KiroCrew/pull/14992 -- removes the last focus-state rules.
- https://github.com/kirodotdev/KiroCrew/pull/14887 -- the first step, removing the accent.
- https://github.com/kirodotdev/KiroCrew/pull/14568 -- the change that introduced the focus glow.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.
