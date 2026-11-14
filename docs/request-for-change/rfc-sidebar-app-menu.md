---
title: Sidebar app rows get a full actions menu and status marks
status: accepted
author: Pearcekieser, with kirocrew-contributor
created: 2026-11-06
last-audited: 2026-11-06
audited-at: 4f7b1ee782
doc-pr: 31104
implementation-prs: []
tracking-issues: [7883]
supersedes: []
superseded-by: []
---

# RFC: Sidebar app rows get a full actions menu and status marks

- Status: accepted. Pearce (Pearcekieser) requested the full menu and the status marks on [#7883](https://github.com/kirodotdev/KiroCrew/issues/7883). The decision that #7883 left open is which actions the menu carries, and this document records it. A maintainer accepts it by approving the PR that adds this file.
- Tracking issue: [#7883](https://github.com/kirodotdev/KiroCrew/issues/7883)
- Builds on: [#26362](https://github.com/kirodotdev/KiroCrew/pull/26362), which added the right-click menu with Details and Unpin.

## Problem

An installed app in the sidebar Apps list has a right-click menu with two items, Details and Unpin. Every other action that the Library offers needs a trip to Apps, then Library, then the app's tile. The sidebar row also shows nothing when the app has an update or needs attention, so the user must visit the Library to find out.

## Current behavior

Verified at `4f7b1ee782`:

1. `AppRailMenu` in `website/src/shell/nav/appRailMenu.tsx` wraps each installed-app row in a Radix context menu with Details and Unpin. When the registry list fails to load, the menu also shows the failure in an `ErrorNotice` and offers the agent hand-off as an item, so a failed read is not mistaken for "no update". Details shows an arrow glyph when `buildUpdateMap` (`website/src/pages/apps/useAppsData.ts`) has an entry for the app. The Library tile uses the same map for its `updateAvailable` flag.
2. `appNavTarget` in `website/src/appNav.ts` returns no target for an app that is disabled or unavailable. Such an app has no sidebar row, so a row always belongs to an enabled app that loaded.
3. Update runs on the detail page. `useAppActions.updateApp` goes to `/apps/detail/<name>` with the router state `autoAction: 'update'`, and `AppDetailPage.tsx` runs the install or sync with its trust consent. The page accepts the trigger from router state only, because a cross-site link can set a query parameter.
4. Disable and Uninstall run in `LibraryPage.tsx`. Uninstall opens a dialog that previews the dependencies it removes (`api.uninstallPreview`).
5. A row already shows an unread count from one of two sources. An app that pushes its own count through `useNavBadge()` shows that count. Otherwise `appNotificationBadges` (`website/src/appNotificationBadges.ts`) counts the app's unacknowledged bell notes, and `mergeAppBadges` lets the pushed count win.
6. A `GET /api/apps` row carries `backend_status.restart_exhausted` when the app's backend keeps failing to restart. Only the Dev Fleet page shows it. An orphaned app's row has a warn-colored icon and opens the migration page.
7. On touch, the rail's drag sensor lifts a row after a 250 ms hold (`TOUCH_HOLD_ACTIVATION` in `website/src/hooks/useDndSensors.ts`). The menu trigger is disabled during a drag, so a draggable row never opens the menu on touch.
8. More than `APPS_NAV_LIMIT` rows collapse behind an "N more" toggle (`website/src/shell/nav/navRail.tsx`).

## Decision

### Menu

The menu opens on right-click, on Shift+F10 or the context-menu key while the row has focus, and on touch as described under Touch. The items, in this order:

| Item | Shows when | What it does |
|---|---|---|
| Update to v*X* | the update map has a newer version | goes to the detail page with `autoAction: 'update'`, so the update runs with the detail page's log and trust consent |
| Details | always | goes to the detail page with no router state |
| Open in Library | always | goes to the Library and focuses the app's tile |
| Mark notifications as read | the app has unacknowledged bell notes | acknowledges each of them through the bell's own `ackNotification` |
| Disable | always | goes to the Library, focuses the tile and disables the app there. A hint under the label says it stops the app and that the Library turns it back on |
| Unpin | always | unchanged from #26362. A hint under the label says it hides the row only and the app keeps running |
| Uninstall… | the app's lifecycle is not `locked` | goes to the Library and opens its uninstall dialog with the dependency preview |

Separators split the menu into three groups: the navigation items, then Disable and Unpin, then Uninstall… by itself in the danger color. When the registry list fails to load, today's failure notice and agent hand-off item stay as a last group after Uninstall…, so a failed read is still not mistaken for "no update". A row whose backend keeps failing to start gets the same pair in that group, stating the failure in full, because the attention triangle names it only in a tooltip.

Disable and Uninstall run on the Library page because the Library already shows their result, their errors and their undo. A disabled app's row leaves the sidebar, and the focused tile then shows Enable. The sidebar has no notice area of its own. The Library accepts these two requests from router state only, under the rule that the detail page uses for `autoAction`. Uninstall still needs the dialog's confirmation, so a stray click on it costs one Cancel.

Details loses its arrow glyph, because the Update item now names the version in words.

Three items from the #7883 proposal are left out:

- Open duplicates a click, Enter or a tap on the row, which already opens the app.
- Enable never shows, because a disabled app has no row (Current behavior, item 2).
- Settings has no source. No manifest field declares an app's settings page, and an RFC that adds one can add the menu item too.

The Library tile's Sync stays off the menu too. It shows for a gateway app with no pending update and re-copies the app from its source directory, a developer step that the tile and the detail page already offer. The menu's Update item covers the case where a new version exists.

### Status marks

A row can show two new marks beside the existing unread count and job run-state dot. Each mark has a tooltip and an accessible name, and one menu item fixes its cause.

| Mark | Shows when | Label | Fixed by |
|---|---|---|---|
| Update arrow, an accent circle with an up arrow | the update map has a newer version, the same rule as the Library tile | "Update available: v*X*" | Update to v*X* |
| Attention triangle, warn color | the app is orphaned, or `backend_status.restart_exhausted` is set | "Needs migration" or "Its backend keeps failing to start" | a click on the row, which goes to the migration page for an orphaned app, or Disable |
| Unread count | unchanged | unchanged | Mark notifications as read, for a count that comes from bell notes. A count the app pushes itself is the app's to clear. |

On the expanded rail, the marks sit in the row's flex line with the other indicators, so they cannot overlap. On the collapsed rail, the update arrow takes the icon's top-left corner and the triangle takes the bottom-left corner. The existing count dot and run-state dot keep the right-hand corners. The phone chat page's icon rail has no row menu, so it leaves out the triangle for a failing backend, and the nav drawer's rows keep it. On that page a failing backend therefore shows only once the drawer is open. That is accepted, because a triangle on a row with no menu would leave the failure in a tooltip that touch cannot open. The marks read the data that the rail already fetches: the `['registry']` query that feeds the Discover badge, and the `['apps']` cache. The change adds no request.

### Many apps

When rows are hidden behind "N more", the toggle shows one accent dot if a hidden row has an update arrow or an attention triangle. Its label is "A hidden app has an update or needs attention". Unread counts stay on their own rows, as they do today. The menu works the same on every row, including the active row that the overflow pulls into view.

### Touch

A hold on a row lifts it for reorder after 250 ms, as today. The sensor's 5 px tolerance applies only during that delay, and an active drag has none of its own. So the menu opens when the drag ends within 5 px of where the hold started, measured from the drag's own delta. The row then does not move, and the menu opens at the finger. When the finger moves, the row reorders and no menu opens. On a row that cannot be dragged, Radix's own long-press opens the menu.

The phone chat page's 72 px icon rail keeps tap-to-open only. The nav drawer lists the same rows with the menu.

## Not in scope

- A sidebar row for a disabled or unavailable app.
- A restart action, which needs a restart route that the dashboard does not have (#7883).
- The Library's own tile menu, and the kebab hit target (#12572).
- An app's own read access to its notifications (#26412).

## Alternatives considered

- Run Disable in place from the menu. The row would leave the sidebar at once, and a failure would have no surface to show on. The Library already has both.
- Keep only Details and Unpin, as #26362 did, to keep destructive actions out of reach of a stray click. The dialog confirmation and the separate group keep Uninstall safe, and Disable is one click to undo on the tile that it focuses.
- Show a visible "⋯" button on each touch row. It adds chrome to every row on the phone for an action that the hold already reaches.

## Exit criteria

1. A right-click on an installed app's row lists the items above in that order, and each conditional item shows only under its condition. When the registry list fails to load, the failure notice and agent hand-off item follow as the last group.
2. Update to v*X* lands on the detail page with `autoAction: 'update'`. Details lands there with no router state.
3. Disable, Uninstall… and Open in Library land on the Library with the tile focused. Disable disables the app there, and Uninstall… opens the dialog with the dependency preview.
4. The update arrow shows on exactly the rows whose Library tiles show an update. The triangle shows for an orphaned app and for one with `restart_exhausted`. Both have accessible names. The menu of an app with `restart_exhausted` states the failure through `ErrorNotice` with the agent hand-off.
5. A touch hold whose drag ends within 5 px of its start, measured from the drag's delta, opens the menu and leaves the order unchanged. A hold that moves reorders and opens no menu.
6. The "N more" toggle carries the dot when, and only when, a hidden row has an arrow or a triangle.
