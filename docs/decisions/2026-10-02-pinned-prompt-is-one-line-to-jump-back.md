# The pinned prompt is one line to click and jump back, never a stand-in for the row

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-02

## Decision

The pinned-prompt banner at the top of the chat exists for one thing: keep a clamped line of the latest prompt in view so the reader can click it and jump back to that message. The transcript row it points at stays visible and scrolls like every other row; the banner never hides the row, never stands in for it pixel-for-pixel, never folds or shrinks with the scroll, and never needs a ceiling against the composer dock. A prompt taller than the viewport is read in the transcript, not in the banner.

## Why

- The seamless hand-off that https://github.com/kirodotdev/KiroCrew/pull/11462 introduced (row hidden the moment its top crosses the fold, card shrinking line by line as a continuation of the bubble), and that https://github.com/kirodotdev/KiroCrew/pull/13630 and https://github.com/kirodotdev/KiroCrew/pull/15828 then had to repair, made a prompt taller than the viewport unreadable: the row was gone, the card showed only its first `maxH` pixels, and scrolling down only made the card shorter. Three PRs, ~23 files and ~6,900 lines of geometry, tests and capture scripts served an effect nobody asked for and broke the one thing the feature is for.
- The maintainer's words: "这一整套东西就是完完全全的 over engineer，我们置顶留一条只是为了让你可以快读点击跳回去" -- the whole thing is over-engineering; the pin keeps one line so you can quickly click back.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/16340 -- the issue recording the decision and the maintainer's direction.
- https://github.com/kirodotdev/KiroCrew/pull/16342 -- the revert of #15828, #13630 and #11462 and the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/11462, https://github.com/kirodotdev/KiroCrew/pull/13630, https://github.com/kirodotdev/KiroCrew/pull/15828 -- the reverted chain.
