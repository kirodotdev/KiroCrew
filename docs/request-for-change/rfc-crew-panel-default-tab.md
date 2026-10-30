---
title: A Crew Mode chat's side panel opens on the Crew tab
status: accepted
author: iamwhatever, with kirocrew-worker
created: 2026-10-30
last-audited: 2026-10-30
audited-at: e106845756
doc-pr: 26002
implementation-prs: [25936]
tracking-issues: [25934]
supersedes: []
superseded-by: []
---

# RFC: A Crew Mode chat's side panel opens on the Crew tab

- Status: accepted on the repository owner's decision of 2026-10-30. Nothing is built on main yet. This document lands first because the change moves a default, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#25934](https://github.com/kirodotdev/KiroCrew/issues/25934)
- Implementation: [#25936](https://github.com/kirodotdev/KiroCrew/pull/25936)
- Amends: UI review r1 rule U1 of Crew Mode ([#25183](https://github.com/kirodotdev/KiroCrew/pull/25183)), "Workers is offered on a Crew Mode slot's strip without taking focus".

## Problem

In a Crew Mode chat (a conductor root, or one of its worker views), opening the right side panel lands on **Changes**. The **Crew** tab, with the crew members and the goal board, is on the strip, but the person has to click it on every visit. The Crew tab is what a Crew Mode user opens the panel for.

## Current behavior

Verified at `e106845756`:

1. When the panel mounts, the pinned-block reconcile (`syncPinned` in `website/src/hooks/usePanelTabs.ts`, called from `usePinnedViews`) writes **Changes** as the strip's focus.
2. `SidePanel.tsx` then calls `offerPanelView(slot, 'workers')` for a chat whose row carries `crew_mode_root` or `crew_root`. `offerPanelView` adds the Crew tab and takes focus only on a strip with no focus at all, so after step 1 it never does. This is rule U1.
3. The strip already remembers each chat's focused tab (`activeId` and its focus history `recent`, persisted per chat in localStorage) and counts tab choices made in this page session (`tabChoices`).

## Decision

U1 is amended:

| Chat | No tab picked | A tab picked |
|---|---|---|
| Crew Mode root | Crew | that tab |
| Crew Mode worker view | Crew | that tab |
| Any other chat | Changes | that tab |

1. A Crew Mode chat's panel opens on Crew while nobody has picked a tab in that chat.
2. "Nobody has picked a tab" is read from the strip's existing state, with no new store or setting: no stored focus, or focus still on Changes with the focus history never having left it and no tab asked for in this page session. A PR chip that opens Changes counts as asked for.
3. A tab the person picks keeps focus on every later open of the panel, as it does today.
4. Every chat that is not Crew Mode keeps Changes as its default, unchanged.
5. The Crew tab still has a close control. Closing it holds until the next visit, and the **+** menu brings it back.

## Alternatives considered

- Keep U1 and rely on the open-panel notice ([#25917](https://github.com/kirodotdev/KiroCrew/pull/25917)), whose button opens the Crew tab. Rejected: it helps only right after a dispatch, not on every later visit.
- Store an explicit "picked" flag per chat. Rejected for this change: the focus history and the tab-choice count already answer the question for every case but one (below), and a new persisted field widens the strip's stored shape.

## Risks

- After a reload, a Changes focus that came from a PR chip and was never moved looks the same as the default, so the panel may open on Crew. The person reaches Changes in one click, and any later pick is kept.
- A chat that becomes Crew Mode while its panel shows the default Changes moves focus to Crew. This is the default this document asks for, applied when the chat gains its Crew tab.

## Rollout

1. This document. Exit: on main with `status: accepted`.
2. [#25936](https://github.com/kirodotdev/KiroCrew/pull/25936). Exit: a Crew Mode root and a worker view open on Crew, a picked tab is kept, a normal chat opens on Changes, and switching between them picks the right default each time, each covered by a test.
