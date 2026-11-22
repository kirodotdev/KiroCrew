# A folder tab in the side panel has Back and Forward through the folders it showed

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-16

## Decision

A folder tab in the chat side panel has Back and Forward buttons at the start of its
own header that step through that tab's folder changes in visit order, the way a
browser does (a new folder change after Back drops the Forward entries), in memory
only for as long as the tab is open in the chat on screen (a chat switch, closing the
tab or a reload starts it fresh) and never shared between chats; the whole side-panel
visit list across tabs is not decided here.

## Why

- It is the first slice the issue itself proposes, and it fixes the "went up too far"
  case.
- The side panel's web preview tab already has Back and Forward with these semantics.
- It changes nothing about the tab strip or how tabs open.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36147#issuecomment-6911724093 -- the
  maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/36147#issuecomment-6844308426 -- the
  three options the decision chose between.
- https://github.com/kirodotdev/KiroCrew/issues/36147 -- the issue.
