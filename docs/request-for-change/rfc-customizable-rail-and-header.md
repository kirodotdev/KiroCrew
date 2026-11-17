---
title: Customizable rail and app header — one permanent Settings menu, everything else arranged by the person
status: draft
author: gjjnn
created: 2026-11-17
last-audited: 2026-11-17
audited-at: ce21fa1f01
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Customizable rail and app header

- Status: draft. Nothing in this document is on main.
- Author: gjjnn
- Related: `rfc-dashboard-chrome-shell.md` (the accepted shell decisions this
  sits beside; see [Relation to recorded decisions](#relation-to-recorded-decisions)),
  `rfc-navigation-placement-seam.md` (where an app asks to be placed),
  `website/src/surfaces/registry.ts` (the registry every rail destination
  comes from)

## Summary

This RFC proposes that the app-wide left rail and the app header (the top bar)
become something each person arranges for themselves.

- **The rail has one permanent item: a Settings menu** at the bottom. One
  click opens a popup holding **Settings**, **Customize** and **Connect your
  phone**. It cannot be moved, renamed or removed.
- **Everything else on the rail is customizable.** A person can remove a
  built-in page such as Sessions or Mates, move it to another place or group,
  and add things the rail cannot hold today: a specific session or a specific
  Mate.
- **The app header is customizable the same way**: its controls can be
  removed and reordered.
- All of this is done in **one edit modal**. Nothing is configured through a
  settings form.

## Motivation

### Current state

Verified at `ce21fa1f01`.

The rail is drawn by `ShellNavRail` in `website/src/shell/nav/navRail.tsx`
from four fixed sections:

| Section | What it shows | How a person can change it today |
|---|---|---|
| Top | Brand and collapse toggle (`RailBrandToggle`), then every `Main` group surface: Sessions, Mates, Schedule, Artifacts | Nothing, except promoting a Customize tab (Skills, MCP Servers and so on) onto it. Those are registered `pinnable` (`capabilitySubItemNav` in `surfaces/surfaceData.ts`) and stored in `mc-nav-pinned` (`lib/navPinned.ts`) |
| Mates | Every starred Mate (`DockMatesSection`, `shell/nav/matesDock.tsx`) | Star or unstar a Mate; drag to reorder. Order is `star_order` on the crew record, stored on the server (`lib/dockMates.ts`) |
| Apps | Discover and Library rows, then installed apps and the former apps Task Runner and Channels | Drag to reorder (`mc-app-nav-order`); unpin from the Library page (`mc-app-nav-hidden`, `lib/appNavHidden.ts`); the list folds after `APPS_NAV_LIMIT` rows |
| Bottom | Terminal toggle, Connect your phone, Customize, Settings, community links (`RailCommunityLinks`) | Nothing |

The header is `ShellHeader` in `website/src/shell/topbar/shellHeader.tsx`.
Left to right on desktop it renders: back and forward (`NavHistoryArrows`), the
remote crew switcher (`CrewSwitcher`, only when a remote crew exists), the
⌘K search trigger, the focus-mode toggle, the readout capsule (connection
state, system metrics, Kiro usage and extension segments from
`apps/capsuleSegments`), the milestones pill, extension widgets from
`apps/topBarWidgets`, the update pill, the feedback pill and the
notifications bell. None of these can be moved or removed by the person.

Four separate preference mechanisms already customize parts of the rail, each
with its own storage and its own editing surface (the rail itself, the Library
page, a Mate's star). Durable `localStorage` preferences such as
`mc-app-nav-order` are backed up to the gateway through `/api/ui-prefs`
(`lib/uiPrefs.ts`, allowlisted in `DURABLE_PREF_KEYS` in
`lib/uiPrefsKeys.ts`), so they follow a person between browsers on one crew.

### Problems

- **The places a person uses most cannot be put within reach.** A session
  someone returns to every day is reached through the sessions list; there is
  no way to keep it one click away.
- **Pages a person never uses cannot be taken away.** Sessions, Mates,
  Schedule, Artifacts and the bottom rows are fixed.
- **Configuration takes three rail rows.** Customize, Settings and Connect your
  phone each hold a row at the bottom, although all three are visited rarely.
- **Customization is scattered.** Reordering apps, unpinning apps, promoting
  Customize tabs and docking Mates are four gestures in three places with four
  storage formats.
- **The header is fixed.** A person who never uses focus mode or the
  feedback pill still gives them header space.

## Goals

- One model for every rail and header item, with one edit modal.
- One shipped default layout. It keeps the rail's top and the header as they
  are today, and leaves the rail's bottom empty apart from the Settings menu.
- The Settings menu is always on the rail, so no layout can lock a person out
  of Settings, Customize or the edit modal.
- A built-in page added in a later release still appears for people who have
  already customized their rail.
- Sessions and Mates can be placed on the rail as items.

## Non-goals

- **The phone header.** Its cells are already fixed per page (the chat page's
  trailing cell holds exactly two controls). It keeps its current shape.
- **Page content.** What a page shows inside the content area is not part of
  this.
- **Themes and colours**, including the theme decorations a theme draws into
  the header.
- **Sharing a layout** between people, or an administrator pushing one.
- **The right side panel** and its toggle.

## Design

### The permanent Settings menu

The Settings, Customize and Connect your phone rows leave the rail. In their
place, the bottom of the rail holds one **Settings** item that opens a popup
beside the rail:

- **Settings** (`/settings`)
- **Customize** (`/capabilities`)
- **Connect your phone** (opens the connect dialog, as the rail row does
  today; shown only when `hasRenderableMobileConnect` is true)
- **Edit rail and header…** (opens the edit modal). Proposed; see open
  question 1.

Properties:

- **It is not part of the layout record**, so no edit and no corrupt record can
  remove it. The shell draws it in a fixed place.
- **It carries every badge its entries carry.** Today the Settings row shows a
  dot for an available gateway or desktop update (`settingsDotLabel`, drawn by
  hand in `navRail.tsx`) and the Customize row shows the count of shared MCP
  servers waiting for approval (mirrored into `appBadges['capabilities']` by
  `shell/nav/railBadges.ts`). The menu item shows a dot when either is set, and
  each entry inside the popup shows its own.
- **It keeps its find-UI locations.** `uiLocation('shell.connect-phone')` and
  the Settings and Customize anchors move into the popup, and the guide steps
  that point at them open the popup first.

### What is customizable

Every other rail row becomes an **item** in the person's layout:

| Item kind | Examples | Notes |
|---|---|---|
| `page` | Sessions, Mates, Schedule, Artifacts, Discover, Library, a Customize tab (Skills, MCP Servers, …), Task Runner, Channels | Any surface the registry would advertise. `hiddenFromNav` surfaces (Notifications, Webhooks) are not offered |
| `app` | An installed app | Keeps its badge, run state and pop-out action |
| `session` | One specific session | Opens that session; shows running and unread state |
| `mate` | One specific Mate | Shows the Mate's face and busy state, as a docked Mate row does today, and opens their chat |
| `action` | Terminal toggle, community links | Rows that do something instead of navigating |
| `collection` | Starred Mates, Installed apps | A live list, not one item: it expands to every member in its own order, so a newly installed app or newly starred Mate appears without an edit |

Items sit in **groups**. A group has a title the person types and an ordered
list of items. An expanded rail shows each group's title; a collapsed rail
separates groups with a divider. Rows still collapse to icons exactly as they
do today.

A session item and the sessions list's own pin (`pinned` on the session row,
`types/index.chat.ts`) are separate: placing a session on the rail does not
pin it in the list, and the reverse. A rail item may carry a person-chosen
label that renames it on the rail only.

The `Starred Mates` and `Installed apps` collections are how the current
rail's Mates and Apps sections are expressed in the new model, so the star
keeps meaning what `lib/dockMates.ts` says it means today and a Library unpin
still removes an app from the `Installed apps` collection.

### The app header

The header uses the same model. Instead of groups it has three locations,
matching the three clusters `ShellHeader` already lays out: **leading** (the
left cluster), **center** (the search track) and **trailing** (the right
cluster, `.tb-right`). Its items are the controls listed under
[Current state](#current-state): back and forward, the search trigger, focus
mode, the readout capsule, the milestones pill, each extension widget, the
feedback pill and the notifications bell. Each can be removed, reordered, or
moved to another header location.

Some header items are conditional system signals rather than places to go.
This RFC proposes that two of them stay outside the layout, like the Settings
menu: the **connection state** (the capsule's plug glyph, which is the only
desktop signal that the gateway is unreachable) and the **update pill** (the
only place a download's progress shows). If the capsule is removed, the
connection glyph stays as a standalone dot. See open question 2.

The remote crew switcher is also proposed as permanent, because choosing the
wrong crew sends work to the wrong machine. It already renders nothing unless a
remote crew exists.

### The default layout

There is one shipped **default layout**. A person who has never opened the
edit modal sees it, and **Reset to default** returns to it. It fills every
location except the rail's bottom:

| Location | Default contents |
|---|---|
| `rail.top` | Group **Workspace**: Sessions, Mates, Schedule, Artifacts, any Customize tab the person already promoted, then the `Starred Mates` collection. Group **Apps**: Discover, Library, then the `Installed apps` collection |
| `rail.bottom` | **Nothing.** Only the permanent Settings menu is drawn there |
| `header.leading` | Back and forward |
| `header.center` | The search trigger |
| `header.trailing` | Focus mode, the readout capsule, the milestones pill, extension widgets, the feedback pill, the notifications bell |

The default `rail.bottom` is empty on purpose: the bottom of the rail is the
Settings menu's place, and nothing else competes with it until the person puts
something there. The terminal toggle and the community links, which sit at the
bottom of the rail today, are not in the default layout. They wait on the edit
modal's shelf as `action` items. The docked terminal stays reachable from its
keyboard shortcut (`lib/terminalChordFocus.ts`), and Report a problem's place
is open question 6.

The default for an existing install is this layout with the person's current
preferences applied: their app order and unpinned apps
(`mc-app-nav-order`, `mc-app-nav-hidden`), their promoted Customize tabs
(`mc-nav-pinned`) and their starred Mates in `star_order`. The `rail.top` they
see is therefore the rows they see today.

### The edit modal

One modal edits both the rail and the header. It is opened from the Settings
menu and from a right-click on empty rail space.

- **Rail.** Each group is a card whose title is typed into directly. Items are
  dragged within and between cards. A small × on hover removes an item or a
  group. A new group is added by dragging an item below the last card.
- **Add shelf.** A side shelf lists what can be added: pages, apps, actions,
  the two collections, and a search box for sessions and Mates. Entries are
  dragged from the shelf onto the rail.
- **Header.** A horizontal strip split into the leading, center and trailing
  locations. Items are dragged within and between them and removed with ×.
  Removed items return to the shelf.
- **Rail bottom.** Below the rail's cards, a drop area for items anchored to
  the bottom of the rail, above the Settings menu.
- **Reset to default** clears the record.

Whether changes apply as they are made or on **Done** is open question 3.

The rail itself keeps its direct gestures: rows can still be dragged to
reorder, and right-clicking a row offers Rename on rail, Remove from rail and
Move to group. These write the same record the modal writes.

### The layout record

One record per person. Every item in it sits at a **location**, so moving an
item anywhere, not only reordering it, is a change to the record.

**Locations** are a fixed set the shell defines and draws:

| Location | Where it is drawn | Holds |
|---|---|---|
| `rail.top` | The rail under its brand row, scrolling when it overflows | Groups of items |
| `rail.bottom` | The bottom of the rail, directly above the Settings menu (where the terminal toggle and community links sit today) | Groups of items |
| `header.leading` | The header's left cluster, after the crew switcher | Items |
| `header.center` | The header's center track | Items |
| `header.trailing` | The header's right cluster, before the update pill and bell area | Items |

An item's place is fully described by three things: its **location**, its
**group** (rail locations only) and its **position**. Position is the item's
index in its list, and a group's position is its index in its location. The
record stores lists rather than numeric `order` fields, so two items can never
claim the same slot and a move is one remove plus one insert.

```json
{
  "version": 1,
  "locations": {
    "rail.top": { "groups": [
      { "id": "g-workspace", "title": "Workspace", "items": [
        { "kind": "page", "id": "chat" },
        { "kind": "session", "id": "<session key>", "label": "Release prep" },
        { "kind": "collection", "id": "starred-mates" }
      ] }
    ] },
    "rail.bottom": { "groups": [
      { "id": "g-tools", "title": "", "items": [ { "kind": "action", "id": "terminal" } ] }
    ] },
    "header.leading": { "items": [ { "kind": "control", "id": "history" } ] },
    "header.center": { "items": [ { "kind": "control", "id": "search" } ] },
    "header.trailing": { "items": [ { "kind": "control", "id": "focus-mode" } ] }
  },
  "seen": ["chat", "members", "schedule", "artifacts"]
}
```

**Each item kind declares the locations that accept it.** `page`, `app`,
`session`, `mate` and `collection` items are accepted in both rail locations;
`control` items (the header controls) only in the three header locations;
`action` items in all five. The modal shows a location as a drop target only
for an item it accepts, and the parser drops an item found in a location that
does not accept it. Whether rail items may also go in the header is open
question 4.

Rules:

- **Each item appears at most once.** An item id is unique across all
  locations, so a page cannot sit in the rail and the header at the same time.
  A parsed record that repeats an id keeps the first occurrence.

- **Absent means default.** With no record saved, the rail and header render
  the [default layout](#the-default-layout), with the person's current
  preferences applied.
- **New built-ins still arrive.** `seen` lists every default item id the
  record has been reconciled against. A default item missing from `seen` is
  inserted at its default location and group (at the end of that group, or a
  new group at the end of the location if the person deleted it) and added to
  `seen`. An item the person
  removed stays in `seen`, so it is not added back.
- **Unavailable is not removed.** An item that cannot render right now (a
  session deleted, an app uninstalled, a preview flag off, a Mate the guest
  session may not see) is skipped but keeps its location, group and position,
  and returns there if it comes back. The modal shows it greyed with its ×.
- **The Settings menu and the permanent header signals are never in the
  record.** There is no id that names them.
- **Forgiving parse.** A record with an unknown `version` or one that fails to
  parse renders the default, never an empty rail.
- **Storage.** The record is one durable UI preference, added to
  `DURABLE_PREF_KEYS`, so it is backed up to the gateway through
  `/api/ui-prefs` and follows the person between browsers on that crew. A
  different crew has its own layout.

Once a record exists, the older keys stop being read for the rail. The Library
page's pin toggle writes the `Installed apps` collection's hidden set in the
record instead of `mc-app-nav-hidden`.

### The phone

The phone nav drawer renders the same body as the desktop rail
(`navBody` in `navRail.tsx`), so it shows the person's rail layout too. Its
phone-only rows (Search and the Kiro account entry) stay fixed above the
Settings menu, because the phone has no other entry to either.

## Relation to recorded decisions

- **`rfc-dashboard-chrome-shell.md`** (accepted 2026-10-02) decides in
  decision 3 that the desktop rail is fixed-collapsed. Its implementation,
  [#16052](https://github.com/kirodotdev/KiroCrew/pull/16052), closed unmerged
  on 2026-11-01, and the desktop rail is still expandable on main
  (`effectiveCollapsed` in `shell/appShell.tsx`). This RFC works either way: an
  expanded rail shows group titles and a collapsed one shows dividers. It does
  not reopen that decision.
- **`rfc-navigation-placement-seam.md`** (draft) proposes that an app's
  manifest `ui.sidebar.section` and `order` choose its rail placement. Under
  this RFC that placement decides where the app lands in the **default**
  layout. Once a person has a record, their placement wins.
- No entry under `docs/decisions/` records the placement of the Settings,
  Customize or Connect your phone rows.

## Migration plan

Each phase is one pull request and leaves the app working.

1. **Settings menu.** Replace the Settings, Customize and Connect your phone
   rows with the permanent Settings menu. Exit criteria: the three rows are
   gone from the desktop rail and the phone drawer; the menu shows the update
   dot and the MCP-waiting badge; the find-UI guides for all three entries
   still reach them.
2. **Layout model and renderer.** The rail's top renders from the record, and
   with no record it renders the default layout. Exit criteria: with no record
   saved, `rail.top` renders the same rows, in the same order, as before the
   change, for a fixture covering reordered, unpinned and promoted items and
   starred Mates; a corrupt record renders the default.
3. **Edit modal for the rail** with pages, apps, actions and collections, plus
   the rail's right-click menu. This phase also moves the terminal toggle and
   community links off the rail's bottom and onto the shelf, so they leave the
   default only once a person can add them back. Exit criteria: with no record,
   `rail.bottom` holds nothing but the Settings menu; every change made in the
   modal survives a reload and a second browser on the same crew; Reset
   restores the default.
4. **Sessions and Mates as items.** Exit criteria: a placed session shows its
   running state and opens it; a deleted session's item is skipped and kept.
5. **The header** joins the record and the modal. Blocked on open question 2.
   Exit criteria: with no record, the header is unchanged; a removed control
   stays removed after a release adds a new one.

## Backward compatibility

- An install with no record keeps the rail's top rows and the header as they
  are. The rail's bottom changes for everyone: Settings, Customize and Connect
  your phone fold into the Settings menu, and the terminal toggle and community
  links move to the edit modal's shelf.
- The existing keys are read to compute the default, and are left in place, so
  a rollback to a build before this change still has them.
- Extension widgets and capsule segments registered through
  `apps/topBarWidgets` and `apps/capsuleSegments` keep working; each becomes
  a header item that defaults to on.

## Security considerations

- The record is presentation only. A session or Mate item is a reference that
  is looked up against live data each render; placing one grants no access, and
  a guest session that may not read the roster (`isNotOwnerRefusal` in
  `matesDock.tsx`) skips Mate items.
- Person-chosen labels and group titles render as text, never as markup.
- The record rides the existing `/api/ui-prefs` backup and has the same trust
  as every other durable UI preference.

## Alternatives considered

- **Keep the star and pin flags as the only switches**, so every starred Mate
  and pinned session appears on the rail. Rejected as the whole model: a pin
  would then always cost a rail row, and a rail label could not differ from the
  session's own title. It survives as the `Starred Mates` collection.
- **A settings form** listing every item with checkboxes and arrows. Rejected:
  arranging is faster and clearer done by dragging the items themselves.
- **No permanent item**, with Reset as the way back. Rejected: a person who
  removed Settings would need to know a hidden path to recover.
- **Keep Settings, Customize and Connect your phone as three permanent rows.**
  Rejected: they are rarely visited and would cost three rows of a rail that is
  now the person's to fill.

## Open questions

1. **Where the edit modal opens from.** This draft puts an entry in the
   Settings menu and on the rail's right-click. Should the menu hold only the
   three entries named in the summary?
2. **Permanent header items.** Are the connection state, the update pill and
   the remote crew switcher permanent, or customizable like every other header
   control?
3. **Save model.** Apply each change as it is made, or preview behind the modal
   and save on Done with Cancel available?
4. **Rail items in the header.** Should the header locations also accept
   `page`, `session` and `mate` items (for example a favourite session as a
   header button), or only header controls and actions?
5. **The rail's top.** Is the brand and collapse toggle part of the rail's
   frame, as this draft assumes, or a customizable item?
6. **Report a problem.** The community links are not in the default layout, so
   Report a problem has no default place on the rail. Should it go in the
   Settings menu?
