# Command Bar Module

## Overview

Command Bar is a builtin App Store app (`kiro_crew/apps/builtins/command_bar/`) that replaces
the dashboard's quick-search surface with a launcher: the reader types a command rather than a
query. It is the first app to ship with **no backend at all** — no subprocess, no port, no
proxy, no routes. Its whole surface is a code-split React chunk in the dashboard bundle, so it
carries the same origin, i18n catalogs, design system and build as the shell.

The app id is `command-bar`; the display name is "Command Bar". `defaultEnabled` is false, and
the app's enabled state IS the opt-in — there is no config key for this feature. While it is
disabled the legacy command palette owns the gesture and nothing about that path changes.

What makes the app worth existing is a single invariant: **the first page issues no network
request.** The palette it replaces ran an unindexed scan over the sessions corpus on every
keystroke, so fast typing could stall unrelated streaming. Command Bar's root carries only
locally-known rows — commands, app destinations, system settings — and searching sessions is a
view the reader ENTERS, so the expensive work is explicit and chosen.

## Responsibilities

1. **Claim the slot** — declare `ui.overlays` in the manifest and take over the `quick-search`
   host slot while enabled, without the shell ever naming an app
2. **Root index** — build the command / app / settings rows from local data only, rank them,
   and cap each group
3. **Ranking** — fuzzy match against the live query plus a frecency boost, so habit surfaces
   without out-ranking a clearly better string match
4. **Scopes** — enter a sub-surface (today: session search) as a navigation state, with its own
   engine loaded on entry
5. **Fallback** — when the root cannot answer, offer the row that carries the query into the
   sessions view rather than reporting "no results"

## The overlay seam

`ui.overlays` is a manifest array of `{id, replaces}`; both fields are required and
kebab-validated (`_OVERLAY_SLUG_RE` in `apps/manifest.py`). `replaces` names a HOST SLOT, and
the only slot that exists is `quick-search` (`HOST_OVERLAY_SLOTS` in
`website/src/apps/overlayRegistry.ts`).

Resolution lives in `website/src/apps/overlaySlots.ts`. `resolveSlotOverlays` accepts a claim
only when all of these hold:

| Requirement | Why |
|---|---|
| the app is enabled | the enabled state is the opt-in |
| the id is in `BUILTIN_OVERLAY_REGISTRY` | the shell must have a component to render |
| `origin === 'builtin'` | the only unforgeable signal (see below) |

The provenance check is load-bearing, not decoration. `register_external_app` takes `source`,
`origin`, `resources` and `lifecycle` as caller parameters and refuses only `origin ==
"builtin"`, and it never runs `_validate_source_path`. Without the check, a self-managed app
could persist a manifest declaring `id: "command-bar"` and take over the gesture WHILE Command
Bar itself was disabled. `origin` is stamped by `discover_builtin_apps()` and reaches
`InstalledApp(origin="builtin")` in `apps/manager.py`, which is why it is the field to trust.

Manifest data from a third party is untrusted input, so a bad declaration is warned about and
skipped with a plain `console.warn` — never through the seam-collision helper, which throws in
dev and test. Installed (non-builtin) apps declaring `ui.overlays` are refused at install time
in `apps/manager.py`.

A builtin must not declare both `ui.overlays` and `ui.entry`: an app with an entry is
downgraded to `local` origin on restart, and would then be refused its own slot. A test pins
that.

## Root rows

`website/src/apps/command-bar/rootIndex.ts` owns the row model.

- `ROOT_GROUPS = ['commands', 'apps', 'settings']`, rendered in that order. `rankRootRows`
  ends with a sort on `groupOrder`, so groups are always contiguous blocks under their own
  header — they never interleave by score.
- A row's `kind` is `view` (enter a surface inside the bar), `navigate` (leave and route), or
  `invoke` (run and close).
- App rows are derived from the installed-app list, so a newly installed app appears as a
  destination with no per-app work.
- Each row renders its kind as a right-aligned word — Command, App, Setting or View — because
  the only other per-row signal is the group icon, which reads only to someone who has already
  learned it. `view` is named separately from its group because it opens a surface instead of
  acting and closing.
- `PER_GROUP_LIMIT = 6` caps each group so one group cannot push the others off the page.
  **Known gap:** rows past the cap are dropped silently.

## Ranking and frecency

`website/src/apps/command-bar/frecency.ts` keeps a per-browser usage map in `localStorage`
with a 14-day half-life, read through a guarded accessor (a disabled or full store degrades to
no boost rather than throwing). `FRECENCY_WEIGHT` is sized so habit beats a marginally better
string match but not a clearly better one: an exact prefix hit on a never-used row still wins
over a scattered subsequence on a daily one.

The root ranks from the LIVE query, not a debounced copy, so a fast typist never sees rows
that answer an older prefix.

## Keyboard and focus contract

- The gesture is the host's quick-search chord; the topbar trigger's label, `aria-label` and
  `title` all follow slot ownership, so it never promises a corpus search the launcher does not
  do.
- Escape is owned by the dialog, not the input, so it works from any focusable child. In a
  scope the first Escape pops back to the root and only the second closes.
- The input is `role="combobox"` with `aria-activedescendant`; rows are `role="option"` with
  `aria-selected`. Arrow keys move the active option without moving DOM focus.
- Because the input is focused for the whole life of the dialog, a `focus-visible` utility on
  it would never turn off, so the cue lives on the active OPTION. The one state with no option
  to highlight is an empty scope (`rowCount === 0`), and there the field carries the ring
  instead — a keyboard user is never left with no cue.

## Invariants pinned by tests

| Invariant | Where it would break |
|---|---|
| the root issues no request | a provider constructed at mount can subscribe a query even when the root never calls it |
| the root ranks from the live query | a debounced read discards a fast-entered query |
| `aria-modal` and the focus trap travel together | a dialog that traps nothing while claiming modality |
| the `apps` query is a pure cache consumer (`enabled: false`) | a second identical fetch per open |
| every `['apps']` reader goes through the one api call | a divergent shape silently poisons the shared cache |
| no builtin declares both `ui.overlays` and `ui.entry` | origin downgrade on restart refuses its own slot |
| a rejected lazy chunk falls back to the legacy palette | the gesture dead-ends after a bad deploy |

## Launch prerequisite

The app is reachable in the UI only once the published catalog carries a row for it. The Apps
page builds its Discover shelf from the network-fetched catalog, and its Library list filters
out disabled builtins, so a default-off builtin with no catalog row appears on neither surface
and cannot be switched on from the dashboard. This is a publishing step outside this
repository, not a code defect — but until it lands, the opt-in exists only over the API.

## Deliberately not here

- **Session search on the root.** Removed on purpose; it is the cost the app exists to avoid.
- **Quicklinks.** A group with no writer was removed rather than shipped empty.
- **App-contributed commands.** An app can appear as a destination today, but declaring its own
  commands needs `contributes.commands`, which this module does not yet read. Until then the
  bar's own command rows are hand-declared, and they can drift from the palette's
  `actionsProvider`.
- **A default-on launcher.** Flipping the default and deleting the legacy palette is a separate
  change, after the remaining corpora become apps with their own scopes.
