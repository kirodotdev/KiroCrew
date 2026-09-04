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
- A row's `kind` is `view` (enter a surface inside the bar), `navigate` (leave and route),
  `invoke` (run and close), or `prompt` (a contributed command -- collect one argument if it
  declares one, then seed a session).
- App rows are derived from the installed-app list, so a newly installed app appears as a
  destination with no per-app work.
- Each row renders its kind as a right-aligned word — Command, App, Setting or View — because
  the only other per-row signal is the group icon, which reads only to someone who has already
  learned it. `view` is named separately from its group because it opens a surface instead of
  acting and closing.
- `PER_GROUP_LIMIT = 6` caps each group so one group cannot push the others off the page.
  **Known gap:** rows past the cap are dropped silently.
- `idleDemote` sorts a row to the end of its group while the query is EMPTY, at a cost sized
  to lose to a single real use. The empty-query order is frecency, so on a cold install every
  score is zero and the alphabet alone decides what the launcher opens on. It is DERIVED, not
  declared: a command that needs an argument cannot act on an empty query, so it has nothing
  to offer a bar that has just opened -- leaving it to the manifest would mean asking every
  app author to volunteer their own row out of the first page, which none would.

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
- **Quicklinks.** A group with no writer was removed rather than shipped empty.
- **A default-on launcher.** Flipping the default and deleting the legacy palette is a separate
  change, after the remaining corpora become apps with their own scopes.
