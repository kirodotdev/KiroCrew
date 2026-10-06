# Command Bar Module

## Overview

Command Bar is a builtin App Store app (`kiro_crew/apps/builtins/command_bar/`) that replaces
the dashboard's quick-search surface with a launcher: the reader types a command rather than a
query. It is the first app to ship with **no backend at all** — no subprocess, no port, no
proxy, no routes. Its whole surface is a code-split React chunk in the dashboard bundle, so it
carries the same origin, i18n catalogs, design system and build as the shell.

The app id is `command-bar`; the display name is "Command Bar". `defaultEnabled` is TRUE, so a
fresh install has the launcher on the quick-search gesture and there is no config key for the
feature -- the app's enabled state is the switch. Builtins otherwise ship default-off, so this
exemption is declared where every other one is, on `manager._DEFAULT_ON_BUILTINS`; the manifest
flag alone would fail the opt-in policy tests. Disabling the app hands the gesture back to the
legacy command palette, which is left in place precisely so that is a real choice rather than a
downgrade; nothing about that path changes while the app is off.

The flag reaches FRESH installs only. An install that registered `command-bar` while it was still
default-off keeps `enabled: false` forever, and because this app has no page it appears in neither
the launcher's own app list nor Discover — so those users have no way to learn it exists.
`manager.backfill_default_on_builtins()`, which reads `manager._DEFAULT_ON_BACKFILL` and records
delivery on the app's own record (`InstalledApp.defaultOnBackfilled`, written in the same atomic
write that flips `enabled`), is what delivers the launcher to them; see app-kit-platform section 12.

What makes the app worth existing is a single invariant: **the first page issues no network
request.** The palette it replaces ran an unindexed scan over the sessions corpus on every
keystroke, so fast typing could stall unrelated streaming. Command Bar's root carries only
locally-known rows — sessions awaiting a user decision, commands, app destinations, and system
settings — and every corpus search is a view the reader ENTERS, so the expensive work is explicit
and chosen.

Four such views exist: session search, artifact search, folder search, and crewmate search —
the last of them behind the `PREVIEW_CREW` flag, because it is an INGRESS to `/members` and
that page is registered with the same flag. Every other door to it applies the gate, and this
app ships `defaultEnabled: true`, so a launcher row that skipped it would advertise a page the
operator has not opted into, on a default install. All three rows the feature adds are gated —
the view row, the query-carrying fallback row, and the empty-state row — because each is
independently reachable, and both memos that build them take the flag as a dependency so
turning the preview on reaches the bar without a reload. The artifacts view
asks `GET /api/artifacts?q=<query>` and nothing else — no `content=1`, no `snippet=1` — so the
server matches NAMES only (`name_contains` in `api_artifacts_list`) and never opens a stored
body. Searching what is INSIDE an artifact is a later change, and it is a change to that one
request.

The folders view is the cheapest of the four, and its shape follows from that. Its corpus is
the folder tree the sidebar already holds under `['chat-folders']`, so a keystroke costs a local
filter rather than a request. The view's own query key nests under that prefix
(`['chat-folders', 'command-bar', 'view', sort, q]`), so every sidebar folder write that
invalidates `['chat-folders']` invalidates the view too: it has no minimum query length, where the two views above each
hold their first characters back, and no row cap, because the count is the reader's own filing
rather than a corpus that grows on its own. Entering the view pays for at most one folder read,
on a cold cache. The folder list used to be spread through the root as its own group instead —
demoted and capped while the query was empty, so the feature read as missing, and competing with
commands once it was not.

That corpus lives in THIS app (`apps/command-bar/foldersProvider.ts`), and the host palette
carries no Folders tab. Reaching a folder by name is a launcher capability, so the launcher owns
it: the alternative is two implementations of the same gesture, one in the app and one in the
surface the app replaces, differing over ranking and reveal and answering to nobody. The corpus
is hook-free for the same reason the session and artifact engines are — the React-Query fetch and
the `usePaletteActions` route change are wired in `CommandBarOverlay`, which is the only thing
holding this app's seams. It still renders the host's `Result` row contract, because forking the
row shape would fork the Enter matrix with it.

The crewmates view (`apps/command-bar/matesProvider.ts`) reaches the reader's own crew: typing
filters the roster by fuzzy name and Enter lands on that mate's pinned DM thread
(`/members?member=<name>`, encoded — a crew may be called `Review & QA`). It is a VIEW rather
than a root group, and the roster's size is the reason it looks like it should be the other way
round. The deciding fact is that the corpus is a FETCH: `GET /api/agents/catalog`, which
`useAgents` calls on mount and which no WebSocket push seeds into the cache. A root group would
therefore either issue that request on every open of the bar — the one invariant this app
exists for — or subscribe to a cache nothing fills and render an empty Crewmates group on a cold
install, which reads as the feature being absent. Like the folders view it has no minimum query
length and no row cap: once the catalog resolves, a keystroke is a local filter over the
reader's own crew.

Two fields of a row come from different places, and only one of them is cached. Identity,
label, role and avatar are the catalog's; whether the mate is WORKING is read from the live slot
frames the root's attention group already uses (`mode === 'member'` and
`running || subagents_running`), keyed on `slot.agent`, which a member slot is born with pinned
to its crew's exact name. That running set is part of the view's query KEY, not merely a
dependency of the engine: rebuilding the engine does not re-run a resolved query, so without it
a mate that had finished would keep its busy dot for the rest of the stale window. The same key
carries `placeholderData`, which is the cost of that decision paid back — this is the only view
whose key moves on an event the reader did not cause, and without held-over rows a member slot
finishing anywhere in the dashboard blanks the list and the selection clamp pulls the reader's
keyboard selection back to row 0 mid-typing. The indicator itself is the dot, colour, pulse AND
WORD the bar already shows for a running session (`recentsProvider`'s own key): "agent is busy"
is one state, the sessions view is one keystroke away, and two words for it left a reader unable
to tell whether they meant the same thing. Only members are listed — the catalog also answers
with installed templates, and a template has no DM thread, so a row for one would navigate to a
page that cannot open it. The built-in `default` assistant is excluded for the same reason
in a second shape: it arrives tagged as a member, and the Crewmates page treats a roster
holding only `default` as EMPTY — it draws the empty-roster hero, never opens a thread for
it, and skips it when choosing which crewmate to open. It is also what a fresh install has
instead of a crew, so listing it would make the first thing a new reader sees in this view
a row that cannot be opened, in place of the empty state that tells them where to make one.

Matching is on NAMES only, over two fields: the displayed label and the immutable `name` a
display label hides, the latter penalised so the word on screen always outranks the one behind
it. When the IDENTITY is what matched, it takes the row's second line, highlighted, in place of
the role — otherwise the row cannot explain itself, and typing `qa` surfaced a row titled
"reviewer" with no highlight anywhere that read as a wrong result. That is the answer
`foldersProvider` already gives when an ancestry path is why a folder surfaced, with one
difference that matters: the identity is GLOSSED ("also called qa-bot"), because a bare second
name under a different title reads as a possible second crewmate, where `kirocrew › oss`
self-evidently reads as a path. The highlight offsets are recomputed against the glossed
string, since the gloss shifts every position. The description is rendered on every other row
and deliberately never matched — that would make this a content search over prose, and the row
could not honestly highlight it.

The view's EMPTY state is a row, not a centred sentence. Naming the Crewmates page without a
way to reach it was the dead end — a reader reported that "nothing looks like a link", so the
one thing they came to do had no affordance. As a row it carries both halves the way the
no-match row does (what is true, and what Enter does about it) and the keyboard reaches it
without leaving the list. It is gated on the roster having actually answered, so a cold fetch
draws the skeleton rather than briefly claiming the reader has no crew.

Adding this view cost the `commands` group its shared cap. Six builtin rows (New Session, Toggle
Theme, and one corpus entry each for sessions, artifacts, folders and crewmates) exactly filled
`PER_GROUP_LIMIT`, which silently evicted every app-CONTRIBUTED command from the idle page: the
app installed fine, its manifest was accepted, its row was simply absent. The sixth builtin only
revealed the defect, which is a cap counting the product's own rows against an app's.

So the cap counts CONTRIBUTIONS only, marked by `RootRow.contributed` and set at the single
place a row enters this group from a manifest. A builtin cannot evict an app's row, because
adding one does not change the count — there is no builtin count at which app rows start
disappearing, rather than a boundary moved further out. What that leaves uncapped is the
product's own list, deliberately: it grows only by a change in this repository, where the page
it lands on is reviewed and a row too many is somebody's decision instead of an invisible
eviction.

## Responsibilities

1. **Claim the slot** — declare `ui.overlays` in the manifest and take over the `quick-search`
   host slot while enabled, without the shell ever naming an app
2. **Root index** — build the attention / command / app / settings rows from local data only,
   rank them, and cap each group. Settings rows match through `scoreSettingEntry`, the scorer
   the palette and the Settings search share (label, synonyms, then a description or tab that
   contains the query), through the row's own `match`. Its tier picks the field the hit is
   drawn on (title marks, the matched synonym, or the subtitle span), and a hit off the label
   pays the same alias/subtitle discount as every other row. A settings row is withheld once a
   cached `useSettingsSearchGovernance` answer (`{ fetch: false }`) says its tab does not draw
   it; an unread answer offers it, as on the other searches, and opening the bar still issues
   no request
3. **Ranking** — fuzzy match against the live query plus a frecency boost, so habit surfaces
   without out-ranking a clearly better string match
4. **Scopes** — enter a sub-surface (today: session search, artifact name search, folder search,
   and crewmate search) as a navigation state, with its own engine loaded on entry
5. **Fallback** — for any non-empty root query, offer Ask plus rows that carry the query into
   the sessions, artifacts, folders, and crewmates views; when no root row matches, also offer
   the app page that can disable Command Bar

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

- `ROOT_GROUPS = ['attention', 'recent', 'commands', 'apps', 'settings']`, rendered in that
  order while the query is EMPTY. `attention` is normally absent and contains only live sessions
  whose status is a pill — an approval or answer the user owes, not merely running or unread
  work. On an empty query `rankRootRows` ends with a sort on `groupOrder`, so groups are
  contiguous blocks under their own header. A NON-EMPTY query skips that regroup and comes back
  in score order across every group, because the reader has said what they want and the best
  match is the answer; group order is the decision about what the launcher OPENS on, and
  applying it to a query too made the group the first sort key, so a command matching only
  through its subtitle outranked the app being spelled out. Groups therefore interleave under a
  query, and the section headers are suppressed there — a header per group change would print
  the same word twice over rows it does not describe. Each row names its own kind in its
  right-hand column instead, and the two groups with no kind word (`attention` carries a status
  pill, `recent` its group name) keep one for that reason.
- `recent` follows `attention`: at most `RECENT_SESSION_ROWS = 3` live sessions the reader was
  last in, so "get me back to what I was doing" is a row to press rather than a search to run.
  It is built by the overlay from the same live slot store `attention` reads, so it costs the
  root no request. The rows are the slots left after two exclusions — any slot already lifted
  into `attention` (it is on screen above, carrying more), and any empty untitled new-chat slot
  (that is what New Session is for) — ordered newest-first by `slotRecency` (the later of
  `last_activity_ts` and `last_ts`, with `created` as a fallback), then sliced to the cap. A
  recent row renders a dot status when the session is running and never a pill (a pill means the
  session owes the user something, and every such session has already been lifted into
  `attention`). The full corpus stays one `view` row below, under Search Sessions.
- A row's `kind` is `view` (enter a surface inside the bar), `navigate` (leave and route),
  `invoke` (run and close), or `prompt` (a contributed command -- collect one argument if it
  declares one, then seed a session).
- App rows are derived from the installed-app list, so a newly installed app appears as a
  destination with no per-app work.
- Ordinary rows render a right-aligned kind — Command, App, Setting or View. A contributed
  command prefixes that kind with its app label. While the query is EMPTY an `attention` or
  `recent` row renders no kind label — `attention` shows its live status pill instead and
  `recent` its running dot, because the session's state is more useful than a static "Session"
  label, and the group header above the row already names it. Under a query there is no header
  (see the group bullet above), so a `recent` row whose session is idle — no pill, no dot, no
  kind word — would carry no naming at all; it renders its group's own name,
  `group_recent_sessions`, in that otherwise empty column. An `attention` row needs no such
  substitution: it reaches that group only because its status is a pill, so the column is never
  empty. `view` is named separately from its group because it opens a surface instead of acting
  and closing.
- `PER_GROUP_LIMIT = 6` caps each group so one group cannot push the others off the page;
  settings carry a second, tighter `SETTINGS_WEAK_LIMIT = 2` on WEAK matches whether or not a
  query is present, and `recent` is capped ahead of ranking by the overlay at
  `RECENT_SESSION_ROWS = 3` rather than by this limit. A weak settings match is one that does not
  name the setting — an empty query (every row is weak), or a typed query that is not a substring
  of the title and only survived as a scattered subsequence. The cap on weak matches holds under
  a query for the same reason it holds on the idle page: a word that merely scatters across six
  long setting titles would otherwise spend six rows and push the overlay's recovery rows (Ask
  the agent, Search sessions / artifacts / folders / crewmates) below the fold — and those rows
  are the whole answer to "the root does not search content" for a reader who typed a name. A
  DIRECT settings hit (the query is a substring of the title) is the user naming the setting, so
  it ranks up to `PER_GROUP_LIMIT` like any row and searching settings on their own name surfaces
  every one of them. The full settings corpus stays one `view` row away regardless.
  **Known gap:** rows past a root cap are dropped silently. The artifacts view does not share
  that gap — it renders a `+N more` line under its list, outside the listbox so it cannot become
  an option that Enter does nothing with — and that line is the shape to copy when this one is
  closed.
- `idleDemote` sorts a row to the end of its group while the query is EMPTY, at a cost sized
  to lose to a single real use. The empty-query order is frecency, so on a cold install every
  score is zero and the alphabet alone decides what the launcher opens on. It is DERIVED, not
  declared: a command that needs an argument cannot act on an empty query, so it has nothing
  to offer a bar that has just opened -- leaving it to the manifest would mean asking every
  app author to volunteer their own row out of the first page, which none would.
- `recent` opts out of all of that while the query is empty: `rankRootRows` scores its rows 0,
  declining the frecency boost, and breaks their tie by SOURCE POSITION rather than title, so
  the newest-first order the overlay already sorted them into stands. Neither frecency, idle
  demotion nor the alphabet may reshuffle a recency order that is a fact. Once a query is
  present a recent row ranks on its match like any other row.

## Ranking and frecency

`website/src/apps/command-bar/frecency.ts` keeps a per-browser usage map in `localStorage`
with a 14-day half-life, read through a guarded accessor (a disabled or full store degrades to
no boost rather than throwing). `FRECENCY_WEIGHT` is sized so habit beats a marginally better
string match but not a clearly better one: an exact prefix hit on a never-used row still wins
over a scattered subsequence on a daily one. A settings row's id follows its label, so
`loadUsage` reads a relabelled row's history under the id it has now, through the legacy-id
table its deep links use (`resolveLegacyHighlightId`): two stored ids that land on one row
merge (counts add, the later use wins), and the next save writes the merged map.

The root ranks from the LIVE query, not a debounced copy, so a fast typist never sees rows
that answer an older prefix.

### A stale Enter is latched, not acted on and not dropped

Every SCOPED view ranks from the debounced query, because each one reaches the network, so for
one debounce interval its rows answer the previous query. A keyboard Enter in that window names
an INDEX, and that index means a different row once the rows move under it — which is how typing
`onc` and pressing Enter opened `accountant`. So the activation path all four views share never
acts on the rows in that window. It does not drop the keystroke either: type-a-name-then-confirm
is the launcher's primary gesture, and eating it left a fast typist pressing Enter at a bar that
said nothing. The Enter is LATCHED — held, then fired once, on the rows that answer the query it
was pressed against — so the gesture lands on the right session, artifact, folder or crewmate
instead of the wrong one or none.

The latch remembers the query the reader confirmed and the row they had selected, and it fires
only while both still hold. A further keystroke confirms a DIFFERENT query, which this Enter never
stood for, so it drops rather than committing whatever the reader typed next; an arrow or a hover
that moves the selection aims at another row, so the latch drops rather than opening the row that
happened to be first. Row 0 is the primary gesture — open the best match for the typed query — so
it fires the live top row even though its content swaps to answer the new query; an arrowed
non-top row instead carries the identity it was picked at, and if the new query is already cached
its rows swap under the old indices with no empty frame, so a changed row at that index drops the
latch rather than opening a row the reader never highlighted. The latch fires only at a settled RESULT row. If the live query settles on a
view's synthesized dead end — a no-match row, an empty-roster row, or the retry row a read that
failed WITH NO CACHED ROWS leaves at the top — the latch
DROPS instead: those rows wipe the query or navigate away, which is not what a reader who typed a
name and pressed Enter asked for, and firing one would act on the live query's dead end. A refetch
that fails while the confirmed query's rows are still cached is NOT a dead end — slot 0 stays a
result that answers the confirmed query, and the latch fires it exactly as a fresh Enter would. The list
must be visible-stable first: while it is still fetching, the latch keeps waiting rather than
firing on a settling frame. Every other way out of the window drops the latch too — Escape,
leaving the scope, closing the bar, and a pointer activation all cancel the pending Enter rather
than firing it late.

Three details carry the window test the latch rides on. The comparison is between what the view
WOULD ask for the live query and what it DID ask, not between the two query strings: sessions and
artifacts only search above a floor, so below it every query asks for the same listing, and
comparing strings would freeze Enter on rows that were the correct answer. It binds the KEYBOARD
only — a pointer names its own target, so the row a reader pressed opens what it says, with no
latch, and a pointer activation also drops any latch a prior keyboard Enter left armed. And it
sits above the row-tag switch, not inside the results case: a view's SYNTHESIZED rows are built
from the same debounced query its results are, so a check that covered results alone would let a
latched Enter act on a stale no-match or empty-crewmates row at the dead end where a reader types
another character.

The query matching is not on its own enough for the crewmates view, which holds its previous rows
across a key change (see the running-set key above). That hold is scoped to the running set: rows
are never held across a change of the query, because those answer the previous words while the
debounced query already matches the new ones.

## Keyboard and focus contract

- The gesture is the host's quick-search chord; the topbar trigger's label, `aria-label` and
  `title` all follow slot ownership, so it never promises a corpus search the launcher does not
  do.
- The bar closes when the visible pane changes (`hooks/useCommandPalette.ts`,
  keyed on `host.activeId` in an embedded pane, else `activeId`); a host model
  arriving or disappearing only sets a new baseline, so relay initialization never
  dismisses it. The current-session rows the bar takes from `useRecentsProvider` are ordered
  by `recentsProvider.prepareCurrentSlots`: the empty new session first (one at
  most), then by recency, with no pinned-first ordering — a pin still renders on
  its row.
- Escape is owned by the dialog, not the input, so it works from any focusable child. In a
  scope the first Escape pops back to the root and only the second closes.
- The input is `role="combobox"` with `aria-activedescendant`; rows are `role="option"` with
  `aria-selected`. Arrow keys move the active option without moving DOM focus.
- Because the input is focused for the whole life of the dialog, a `focus-visible` utility on
  it would never turn off, so the cue lives on the active OPTION. The one state with no option
  to highlight is an empty scope (`rowCount === 0`), and there the field carries the ring
  instead — a keyboard user is never left with no cue.
- Opening a session — an attention or recent row on the root, or a row of the sessions view —
  leaves the caret in that session's composer once the bar has closed, the outcome a sidebar
  click has. The bar says so itself (`focusComposerForOpenedSession` /
  `focusComposerForResumedSession` in `composerFocus.ts`) rather than leaning on the composer's
  own autofocus: that effect fires only on a slot-key TRANSITION, so it never runs for the session
  that is already active, and it declines while an editable element holds focus, which the bar's
  input still does when a root row's `switchSlot` commits (the bar closes only once the row's
  promise settles). The caret moves only once the switch has LANDED — the unwrapped
  `switchSlot` has fulfilled and the opened slot is still the active one — never while the
  gateway round trip is in flight: `switchSlot.pending` enters the slot synchronously, so a caret
  placed before the answer would route the keystrokes typed meanwhile to a slot a 404 then
  unwinds and evicts, and the page would file that text under the evicted key. A rejected switch
  gets no focus from the bar: the selection unwinds to the origin, the pane notice explains the
  gesture, and the composer's own autofocus treats that key transition as it treats any other.
  A fulfilment the user has already moved past focuses nothing, as the `switchSlot.fulfilled`
  reducer ignores it. Two switches to the same key can overlap (a second gesture, or the chat
  page's mount-time `switchSlot(activeSlot)` when the bar was used from another page); when the
  older read lands first, the caret waits for the newer claim to settle — placed once it clears
  with the slot still active, dropped when its 404 unwinds the selection. In split view (a
  session-grid pane is mounted) the caret goes to the composer of the pane whose
  `data-pane-slot` equals the opened or resumed key (`queryComposerForSlot`); when no pane
  renders that key, or the gesture named no key, no caret is placed, because every other
  pane's composer belongs to a session the gesture did not open.
  Touch devices are skipped and a collapsed composer stays collapsed, exactly as the sidebar's
  autofocus leaves them; a resume the chat page cannot display focuses nothing.

- ⌘C / Ctrl+C copies the selected row's address (`components/commandPalette/copyTarget.ts`
  `resolveCopyTarget`): a row's address is derived from what it opens, or from an explicit
  `copyUrl` validated as an http(s) URL; a dashboard route that starts with `//` yields no
  address. The bar declines the chord, leaving the browser's own copy, while the input holds a
  text selection or a command argument is being typed. A row with no address answers with a
  notice instead of copying. The footer names the chord only while the selected row has an
  address, and a failed clipboard write shows an error notice naming the address.

## Invariants pinned by tests

| Invariant | Where it would break |
|---|---|
| the root issues no request | a provider constructed at mount can subscribe a query even when the root never calls it |
| the artifacts view matches NAMES only | a `content=1` or `snippet=1` param turns each entry into a read of every stored body |
| the artifacts fallback row issues no request while it is merely OFFERED | offering a way into the corpus becomes a scan on every keystroke, which is the cost this app exists to avoid |
| the artifacts row cap names its remainder, from OUTSIDE the listbox | a silent slice reads as "these are all of them"; a counted row inside the listbox is an option Enter cannot act on |
| the root ranks from the live query | a debounced read discards a fast-entered query |
| the crewmates view is entered, never fetched from the root | the catalog `useAgents` calls on mount becomes a request on every open of the bar |
| the crewmates view lists MEMBERS only | a template row navigates to a page with no thread to open |
| the crewmates view excludes the built-in `default` | a fresh install's first row is one the Crewmates page opens for nobody, in place of the empty state |
| all three crewmate rows sit behind `PREVIEW_CREW` | a default install advertises `/members`, which every other door keeps behind that flag |
| the crewmates read shares `['agents-catalog', 'global']` | two caches of one sessionless response, so the dialog fetches and the bar refetches the same bytes |
| a crewmate route is encoded | a crew called `Review & QA` truncates the parameter and opens the page on nothing |
| a crewmate row is keyed on the immutable `name`, not the display label | a renamed crew routes to a member that does not exist, and reads as idle while it is working |
| the running set is part of the crewmates query key | a resolved query keeps a finished mate's busy dot for the rest of the stale window |
| that query holds its previous rows across the key change | a slot finishing elsewhere blanks the list and resets the reader's keyboard selection to row 0 |
| it holds them across the RUNNING SET only, never across the query | rows answering the previous words stay actionable for as long as the new read takes |
| a scoped-view Enter is latched and fired on the rows that answer the query it was pressed against | a dropped keystroke makes a fast typist confirm into silence, and acting now opens the row selected against an older query |
| the latch drops on a further keystroke, a selection move, or a pointer activation | a latch outliving its confirmation commits a query the reader never confirmed, opens the row they moved off of, or re-fires after a click |
| the latch fires only at a settled result row, and drops at a synthesized dead end | a latch firing a no-match row discards the query just typed, and one firing an empty-roster row navigates away |
| that guard binds the keyboard, not the pointer | a row the reader can read and press stops responding |
| the guard compares what the view WOULD ask against what it DID ask | a sub-floor query freezes Enter on a listing that is the correct answer to it |
| a crewmate row says BUSY in the same word as a session row | two vocabularies for one state, one keystroke apart, that a reader cannot tell apart |
| an identity match shows the identity it matched | a row surfaces with no highlight anywhere and reads as a wrong result |
| that identity is GLOSSED, and its highlight recomputed against the gloss | a bare second name reads as a second crewmate, and carried-over offsets mark the gloss's own words |
| those offsets are shifted by the template's INTERPOLATION SITE | a mate named `cal` glosses to "also called cal" and the highlight marks the `cal` inside "called" |
| the empty crewmates state is an Enter-able row | the view names the Crewmates page with no way to reach it, and nothing looks like a link |
| that row waits for the roster to answer | a cold fetch briefly claims the reader has no crew |
| a failed crewmate read renders an ErrorNotice | the retry row carries no text by design, so the failure is unexplained and indistinguishable from a crew-less install |
| the `commands` cap counts CONTRIBUTED rows only | a builtin added here evicts an app-contributed row with nothing on screen saying so |
| that cap still bounds one app's contributions | an app with twenty commands turns the first page into its index |
| the product's own command list is uncapped | a builtin silently dropped from a page this repository reviews |
| `aria-modal` and the focus trap travel together | a dialog that traps nothing while claiming modality |
| a session opened from the bar ends with the caret in its composer, same-key or not | a keyboard-first switch that ends in a mouse click on the composer (#15732) |
| the caret moves only once the opened session's `switchSlot` has fulfilled; a refused switch gets none from the bar | keystrokes typed during the gateway round trip filed under a slot the 404 unwind then evicts |
| an older same-key read landing first defers the caret to the newer claim | a stale fulfilment focusing while the live read is still out, then its 404 stranding the typed text |
| the `apps` query is a pure cache consumer (`enabled: false`) | a second identical fetch per open |
| the folders view's query key nests under `['chat-folders']` (`CommandBarOverlay.folders.test.tsx`) | a sidebar folder write leaves the bar listing folders that were renamed, moved or deleted |
| in split view the caret goes only to the pane whose `data-pane-slot` is the opened key (`paletteOpenSessionFocusesPaneComposer.test.tsx`) | keystrokes land in a pane bound to a session the gesture did not open |
| every `['apps']` reader goes through the one api call | a divergent shape silently poisons the shared cache |
| no builtin declares both `ui.overlays` and `ui.entry` | origin downgrade on restart refuses its own slot |
| a rejected lazy chunk falls back to the legacy palette | the gesture dead-ends after a bad deploy |
| a malformed contribution is skipped, never thrown | one bad app takes the Cmd+K gesture down for every app |
| a contributed row id is namespaced by its app | a contribution impersonates a builtin row and inherits its frecency |
| an argument the matcher refuses creates no session | a wrong paste reaches an agent told to write somewhere |
| a manifest never supplies its own matcher | a third-party regex on the launcher's thread cannot be bounded |
| a disabled app contributes nothing | the enable switch stops being the reader's control |
| an auto-sending command shows its resolved prompt first | app-authored text is sent that the reader was never shown |
| `autoSend` without an argument is refused, and clamped off | a command that skips the argument state sends with no preview at all |
| `autoSend` is honoured only for the JSON boolean `true` | `"autoSend": "false"` coerces truthy and enables the send |
| an argument carrying `pattern` or an unknown `kind` is refused | a stale-contract app would silently fall back to accepting anything |
| a command that needs an argument never leads an empty query | the alphabet makes a bulk write the default Cmd+K offer |

## Contributed commands

`contributes.commands` is the seam that lets a launcher row live OUTSIDE this
repository. An app declares what the row says and what it does; the host renders and
runs it. This is the answer to "my commands should be my own configuration, not a
patch to the product" — an app that contributes commands needs no page, no frontend
bundle, no backend and no process. A manifest and a skill are enough.

It sits beside `ui`, not inside it, and the split is the whole idea: `ui` is where an
app declares surfaces of its OWN (a page, an overlay it supplies a component for),
while a contribution is a row inside a surface the host owns.

A contribution is **data, not code**:

| Field | What it is |
|---|---|
| `id` | kebab slug; the row id is namespaced `app:<app>:<id>` |
| `title` / `subtitle` | row copy, straight from the manifest |
| `icon` | a name from the host's own glyph set |
| `keywords` | hidden match aliases |
| `argument` | the ONE value the command collects: `kind` (`url` / `text`), `hosts` for `url`, plus `placeholder`, `hint`, `patternError` |
| `prompt` | the action — the text a new session is seeded with, interpolating `{argument}` |
| `autoSend` | send that text immediately rather than leaving it in the composer |

**No app-supplied code, and no app-supplied image.** A contributed function would be
third-party JavaScript running inside the host's own surface, on every keystroke,
with the reader's session; a contributed icon URL would be a network request from the
one surface that promises to issue none. Both are refused for the same reason the
overlay registry resolves `id` against components compiled into the bundle rather
than loading one from the app. The glyph set grows by pull request, which is a cheap
ask next to either alternative.

**The argument declares a matcher by NAME; it never ships one.** The collected text is
spliced into an instruction handed to an agent with tools, so "whatever the reader
pasted" is not an acceptable domain — but the check itself belongs to the host.
`kind` selects one of a fixed set (`url`, with an optional `hosts` allowlist, or
`text`), and the host implements each one.

An earlier revision of this contract let the manifest supply its own `pattern`. That
was wrong in a way worth recording, because the shape of the mistake recurs: a regex
is a small program, and this one ran against the field on every keystroke on the
thread that draws the launcher. `^(a+)+$` and `^(a|aa)+$` are both under ten
characters and both exponential, and neither runtime can interrupt a synchronous
match, so no timeout was available. Screening the pattern syntactically was tried and
abandoned — such a check recognizes shapes, so each version invites the next hostile
pattern it does not cover, and the length cap bounded nothing (eight characters is
enough). The fix was to delete the primitive rather than keep fencing it.

So every matcher now runs in time proportional to the input no matter what a manifest
asks for: `url` uses the runtime's own URL parser, which is linear by construction.
An `argument` carrying a `pattern` key is REFUSED rather than migrated, because
silently dropping it would leave an app written against the old contract running on
`text` — accepting any non-empty string — with auto-send still on. An unknown `kind`
is refused for the same reason: a manifest asking for a check this host does not have
must not quietly receive a weaker one.

The cost is precision, and it is real. A pattern could demand `/pull/<n>`; `url` with
`hosts: ["github.com"]` admits any URL on that host and leaves what the link DENOTES
to the agent reading it. That is the right split — the host is the wrong place to
encode another product's URL taxonomy, and it cannot do so safely — but it is a
reduction in what an app can express, not a free win.

The allowlist is exact unless an entry carries a leading dot (`.github.com` admits
subdomains, `github.com` does not admit `github.com.evil.test`), and only `http` and
`https` are accepted: `javascript:` and `data:` parse as valid URLs, and this value is
shown back to the reader and handed to an agent.

**Validated twice, on purpose.** `AppManifest` checks it on every parse, and
`contributedCommands.ts` re-checks the same rules before rendering. The second pass
is not redundancy: an unknown top-level manifest key reaches the dashboard through
the manifest's `extra` bucket having passed no schema at all, so an app installed by
an older gateway can put an arbitrary object on this path. A bad declaration is
SKIPPED with a warning, never thrown — a malformed app must not take the Cmd+K
gesture down for every other app on the instance.

**Disabled apps contribute nothing.** The enable state is the reader's switch over
the whole app, and a row that still ran from a disabled app would make that switch a
lie. There is no provenance check beyond that, unlike an overlay claim: an overlay
REPLACES a host surface so only a builtin may claim one, while a command ADDS a row
the host renders and runs, which is exactly the capability an external app should
have.

### What the reader sees before an auto-sending command fires

`autoSend` sends app-authored text to an agent with tools as if the reader had typed
it. They chose the row and supplied the value, but nothing had shown them the
instruction itself. So the argument state renders the RESOLVED prompt — the template
with their value already spliced in — once the matcher accepts the value, and Enter
sends that. The preview is withheld until the value validates, so it never advertises
text that is not what would be sent.

**`autoSend` therefore REQUIRES an argument.** The preview is the consent, and it
lives in the argument state; a command that collects nothing never reaches that step,
so honouring `autoSend` there would send app-authored text with nothing shown to the
reader at all — which is precisely what a misleading row in a hostile manifest would
aim for. The manifest refuses the combination outright rather than downgrading it
silently, so the app author learns the rule instead of wondering why it did not fire,
and `contributedCommands.ts` clamps it independently for an app whose manifest reached
the dashboard through `extra` without being validated. Such a command still runs — it
lands in the composer, where the text is visible and one keystroke sends it.

`autoSend` is also honoured only for the JSON boolean `true`. Every non-empty string
is truthy in both languages, so a coercing read would let `"autoSend": "false"` enable
the one capability that sends on the reader's behalf.

That is informed consent at the moment of action rather than a grant dialog at
install time, which is what an argument-taking command can offer: the reader is
already looking at the field. A stronger per-app grant is a reasonable future
addition, not a substitute — a grant given once at install is not read again at the
moment a bulk write actually fires.

### Where the session it opened goes

Every contributed row opens a NEW session, so a reader who uses two of them a few
times a day accumulates generated sessions at the top level of the sidebar, mixed
together and pushing their own chats down. So the host FILES each one under
`Command Bar Sessions / <the row's title>`, creating whichever folder does not exist
yet. One parent says where all of them came from; one leaf per row keeps two commands'
runs apart without the reader sorting anything.

The folders are matched BY NAME on every run rather than remembered by id, because the
launcher holds no per-command state to remember one in. The consequence is deliberate:
renaming or moving a leaf makes it stop matching, so the next run creates a fresh one —
visible and undoable, where a remembered id would silently refile into a folder the
reader had moved on from.

Three rules keep this cosmetic rather than load-bearing:

- **Filed last, and never awaited.** The prompt is already seeded and the bar already
  closed, so a folder API that is slow, capped, rate-limited or refused costs this
  session its place in the sidebar and nothing else. Every failure is swallowed.
- **The folder list is read from the sidebar's cache, not fetched.** `GET
  /api/chat/folders` walks the on-disk session list synchronously to count archived
  sessions per folder, so fetching it per run would pay for a filesystem scan — scaling
  with the reader's history — to learn what the `['chat-folders']` cache already holds.
  The launcher subscribes to that cache the same way it subscribes to the app list. A
  cache HIT is trusted; a MISS is not, because it might only mean the cache has not
  heard about a folder an earlier run created, and a warm cache stale about exactly that
  folder would duplicate it on every run. The first miss spends one authoritative read
  and re-checks before creating anything, so the common path issues no request at all
  and the worst path issues one.
- **Contributed rows only.** The Ask row uses the same seeding path but carries a
  sentence the reader wrote; it belongs wherever they are working, not under a
  command's name.
- **Nothing filed for a seed that was abandoned.** A bar dismissed mid-create sends no
  prompt, and a folder named after a command that did not run is worse than an unfiled
  session.

The parent name is not localized, which is a choice and not an omission: it is written
to the server and matched by that name later, so a translated copy would fork a second
folder the moment the reader switches language and strand every session already filed.
That makes it a durable server-matched value rather than UI copy, so it is written as a
literal and the module carries a scoped `i18next/no-literal-string: off` block in
`eslint.i18n.config.js`, beside `wireValues.ts`'s for the same category — suppression
stays where this repository counts it. Assembling the name at runtime to keep the
scanner from seeing it was tried first and is worse: it opens a third suppression
channel nothing counts.

Both folders are resolved through the shared `ensureChatFolder` helper
(`website/src/utils/ensureChatFolder.ts`), which matches by name under an exact parent and
asks the server only for names it will store unchanged. A leaf name is cut to fit the 100
code points the server keeps, because a manifest title may be 120: an over-long title
keeps its first 65 code points and ends in ` (<32-hex fingerprint of the whole title>)` —
the first 128 bits of its SHA-256, so two different titles cannot be made to share one.
Without the cut the create is silently shortened, the next run's lookup for the full
title misses, and every run makes another folder — the one failure mode here that
compounds rather than staying cosmetic; and the fingerprint keeps two different long
titles that agree on their first 100 code points in two leaves. When two rows currently offered
carry the SAME title, a discriminator is appended to both — the contributing app's label
between two apps, and the row's own id when the collision is inside ONE app, where the
label separates nothing. A leaf keyed on the title alone would interleave two commands
the reader cannot tell apart, which is the very thing this filing prevents; and a
discriminator is appended only on a collision, because always would put a redundant
parenthesis on every folder for a case almost nobody has. The suffix gets its own room
inside the stored limit rather than being appended and clamped after, since clamping the
finished name slices the discriminator off in exactly the case it exists for. Two
commands fired at the same instant can each find the parent missing and create it twice;
the loser's folder is an empty duplicate, which is cheaper than putting a lock in front
of the reader's session appearing at all.

## Switching it off, and back on

Both directions have to work in the UI, and one of them nearly did not. The Apps page
builds its Discover shelf from the network-fetched catalog, which carries no row for this
app, and its Library list hides disabled builtins -- so with the app off it would have
appeared on neither surface and could only be re-enabled over the API. Library therefore
keeps listing a disabled builtin unless its manifest sets `hidden`
(`keepInLibrary` in `website/src/pages/apps/useAppsData.ts`): an app a reader turns off and
then needs to find again has to stay on the one surface that carries Enable, and this app's
own description tells them to disable it to get the old surface back. The rule is keyed on
being installed, not on this app's name, and it lists the other default-off builtins too.

## Deliberately not here

- **Session search on the root.** Removed on purpose; it is the cost the app exists to avoid.
- **Artifact CONTENT search.** The artifacts view matches names only. Reading bodies means
  `snippet=1` on every entry, and a snippet response then needs a payload budget, a sort
  guarantee and somewhere to put "more results" — a contract to design against a response shape
  that does not exist yet, not a parameter to add.
- **Quicklinks.** A group with no writer was removed rather than shipped empty.
- **Removing the legacy palette.** Command Bar is default-on, but disabling it deliberately
  restores the legacy palette and a rejected lazy chunk falls back there. Deleting that fallback
  is a separate change, after the remaining corpora become launcher scopes.
