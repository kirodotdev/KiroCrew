---
title: The developer tools live in Settings > Developer, with no Developer Mode switch
status: accepted
author: iamwhatever
created: 2026-10-27
last-audited: 2026-10-27
audited-at: d8e04b97f3
doc-pr:
implementation-prs: [24810]
tracking-issues: [24797]
supersedes: []
superseded-by: []
---

# RFC: The developer tools live in Settings > Developer, with no Developer Mode switch

- Status: accepted. This is the product owner's decision, given 2026-10-26:
  merge the standalone Developer page into Settings > Developer, make today's
  Settings > Developer content its "Feature Previews" page, and remove the
  Developer Mode toggle after checking what else it gates. The owner reviewed
  the full list of what the toggle gates (below) and chose to keep every one of
  those surfaces, now shown to everyone. Claims about today's code were checked
  at `d8e04b97f3` (main).
- Author: iamwhatever

## 1. Today

At `d8e04b97f3` the per-device `mc-dev-mode` localStorage flag, flipped by the
**Developer Mode** switch in Settings > Developer (`DeveloperPanel.tsx`), gates
five things:

| Gated surface | Where |
|---|---|
| The left-rail **Developer** row and its unseen dot, which open `/developer` | `shell/nav/navRail.tsx`, `App.tsx`, `shell/nav/developerMode.ts` |
| The "Open Developer page" link under the switch | `pages/settings/DeveloperPanel.tsx` |
| The chat side panel's `+` menu rows Logs, Context breakdown and Crew log | `pages/chat/SidePanelAddMenu.tsx` (`DEV_ONLY_VIEWS`), `hooks/useDevMode.ts` |
| The desktop app's View > Toggle Developer Tools item | `electron/app-menu.js` (built hidden), `shell/platform/electronBridge.ts` |
| The `developer_mode` guide gate in front of `shell.developer` | `guide/guidePredicates.ts`, `uiLocations/conditions.ts` |

The `/developer` route itself is mounted for everyone; only its rail entry is
gated. Deep links (`/developer?tab=mcp-pool`, the default-mate badge, curated
tips, the doctor's MCP hint) already reach it without the switch, which is why
the doctor and two docs carry "Developer Mode does not need to be on".

The server's `KIROCREW_DEV_MODE` environment flag and per-app dev mode
(`/api/apps/{name}/dev`) are separate mechanisms and are not part of this.

## 2. Decision

1. **One home.** Settings > Developer becomes a Settings SubNav (the pattern
   Chat, Display and Notifications use). Its first page, **Feature Previews**,
   holds what the tab shows today. The nine Developer page tabs follow it as
   pages at `/settings/developer/<page>`.
2. **No switch.** The Developer Mode toggle is removed. A switch in front of
   pages that are already one click inside Settings guards nothing.
3. **What the switch gated stays, for everyone.**
   - The rail Developer row goes away: the pages are in Settings.
   - Logs, Context breakdown and Crew log stay in the side panel `+` menu,
     always offered. They read the user's own session and change nothing.
   - View > Toggle Developer Tools stays in the desktop menu, always shown.
     It opens DevTools for the local person at the machine, the same reach a
     browser tab of the dashboard already has.
4. **Old links keep working.** `/developer?tab=<page>` redirects to
   `/settings/developer/<page>`, keeping its other query params;
   `/developer?tab=agent-backend` still forwards to Settings > Agent Harness.

## 3. Not decided here

- The server's `KIROCREW_DEV_MODE` flag and app dev mode stay as they are.
- What any internals page shows is unchanged.
- The standalone `/logs` page and the other routed pages stay where they are.

## 4. Readers of what is taken away

`mc-dev-mode` (backup key list), `useDevMode`, `useDeveloperMode`, the
`developer_mode` guide gate, the `shell.developer` and `page.developer` index
entries and the `developer` palette page. Each reader and how it is handled is
listed in the implementing PR's Backwards compatibility section.
