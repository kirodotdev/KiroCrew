# The right SidePanel's workspace tabs are fused browser-style tabs, and the activity-panel toggle lives on the chat title row

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-09-14

## Decision

The right-hand SidePanel renders its workspace tabs in the fused browser-tab style: the active tab shares the panel body's background and joins it, never a row of detached pill chips. The toggle that opens and closes the activity panel sits on the chat title row, not in the top bar and not in a fixed overlay at the workspace corner.

## Why

- The toggle moved from the top bar to the chat title row in https://github.com/kirodotdev/KiroCrew/pull/145 so that opening the panel no longer narrows the full-width header. The fused tabs were the maintainer's own change in https://github.com/kirodotdev/KiroCrew/pull/6491, whose code comment says it deliberately supersedes the earlier Figma "Side Navigation" pill spec, with a pin test (`sidePanelBrowserTabs.test.tsx`).
- https://github.com/kirodotdev/KiroCrew/pull/10119 turned the tabs back into centered pills and moved the toggle to a fixed overlay; its diff removed the superseding comment, the `.side-tab-active` rule and the pin test. The maintainer reverted it whole the next day: "Owner decision: revert #10119 as a whole. The author is warmly invited to re-submit the parts the review found justified in a separate PR, backed by an issue and screenshots of every shipped state."

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/10626 -- the maintainer's revert, whose body carries the decision; closes issue #10619.
- https://github.com/kirodotdev/KiroCrew/issues/10619 -- the issue the maintainer opened for the revert.
- https://github.com/kirodotdev/KiroCrew/pull/6491 -- the fused tabs, with the superseding comment and pin test.
- https://github.com/kirodotdev/KiroCrew/pull/145 -- the toggle's move to the chat title row.
- https://github.com/kirodotdev/KiroCrew/pull/10119 -- the reversal this entry records as a regression.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.
