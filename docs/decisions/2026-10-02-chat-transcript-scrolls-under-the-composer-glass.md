# The chat transcript scrolls under the composer glass at every scroll position

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-02

## Decision

On the main chat page the transcript scroller runs the full height of the pane and the conversation passes under the composer dock's translucent glass at every scroll position, whether or not the jump-to-bottom pill is showing and whether or not the status stack above the composer holds a bar; the scroller pays for the covered strip with `padding-bottom`, never by ending its box above the dock. This covers the transcript only: the welcome hero's suggestion cards and Refresh link are controls and keep ending above the dock.

## Why

- Text visible through the glass is the product (the iOS toolbar layout), not a defect. Legibility of what sits over the transcript is the glass recipe's job (blur and tint), never the scroller's.
- https://github.com/kirodotdev/KiroCrew/pull/15820 read the scroll-under as a bug and clipped the transcript above the dock; the maintainer saw the hard edge on main the same day and reverted that half with the words "这个是一个 design decision", keeping #15820's welcome-hero half ("hero 修复留下").

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/16289 -- the issue recording the decision and the maintainer's direction.
- https://github.com/kirodotdev/KiroCrew/pull/16291#issuecomment-5963169872 -- the maintainer's on-record restatement of the decision, on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/16291 -- the revert of #15820's transcript half and the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/15820 -- the reverted attempt.
