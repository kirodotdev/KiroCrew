---
title: Navigation Placement Seam — honor the manifest contract the rail already promises
status: draft
author: zezhexu
created: 2026-08-16
last-audited: 2026-08-16
audited-at: 2a665e735
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Navigation Placement Seam — honor the manifest contract the rail already promises

- Status: draft — nothing implemented. All three phases are proposals.
- Author: zezhexu
- Created: 2026-08-16
- Related: `rfc-federated-app-platform.md` (the app UI loading path this rides),
  `website/docs/extension-seams.md` (the nine existing edition seams this adds a
  tenth to), `docs/system-specs/modules/app-kit-platform.md` (manifest contract),
  `website/docs/i18n-catalog.md` (the authoring rule §3.3 closes a hole in)

## 1. Problem statement

The dashboard rail already has the right abstraction. `Surface` in
`website/src/surfaces/registry.ts` is a data-driven registry, an installed app loads
its UI through a real dynamic `import()` with no SPA rebuild, and `app.json` already
declares where an app wants to sit. What is missing is the wiring between those
pieces. Verified on main `2a665e735` (2026-08-16); this document cites symbols
rather than line numbers because every line reference taken from a checkout 962
commits behind had moved:

- **The manifest field exists and nothing reads it.**
  `src/kiro_crew/apps/manifest.py::UISidebar` declares `section: str = "Apps"` and
  `order: int = 10`, is a field of `UIConfig` (`UIConfig.sidebar`), round-trips
  through `to_dict`/`from_dict`, and the `ui` key is described in
  `src/kiro_crew/apps/manager.py`. A grep for `ui.sidebar` across `website/src`
  returns **zero hits**, and the object returned by
  `website/src/appNav.ts::appNavTarget` carries neither `section` nor `order`. The
  manifest promises placement control that the frontend silently discards.
- **Only the first page of a multi-page app is reachable.** `appNavTarget` resolves
  `app.manifest?.ui?.pages?.[0]`. `pages` is a list; pages 2..n have no rail
  affordance and no other entry point.
- **There is no ordering field at all.** `Surface` has no `order`. Built-in order is
  registration order in `website/src/surfaces/builtins.tsx`, whose own header says
  "order in this file = order in the rail". The `Apps` group is dnd-sortable with
  the result persisted in `localStorage['mc-app-nav-order']` and truncated at
  `APPS_NAV_LIMIT = 6` (`website/src/App.tsx`), so an app's declared `order: 10` has
  nowhere to land even if it crossed the boundary.
- **`registerBuiltinSurface` is not a seam.** `website/docs/extension-seams.md`
  documents nine edition seams composed through `virtual:kirocrew-edition`;
  `registerBuiltinSurface` is not among them (grep confirms no mention). It is
  core-internal wiring called only from `surfaces/builtins.tsx`. An edition
  therefore cannot contribute a `Main`- or `Bottom`-group entry, and an app cannot
  contribute one at all — an app's single nav affordance is `pages[0]`, always in
  `Apps`.
- **A declared group has no registrants.** `SurfaceGroup` is
  `'Main' | 'Apps' | 'Platform' | 'Bottom'`; nothing registers into `'Platform'`.
- **Nav labels have two standards, and third-party apps get the weaker one.**
  `website/src/i18n/navLabels.test.tsx` gates every `getBuiltinSurfaces()` entry: it
  must carry `labelKey`, and that key must resolve non-empty in every
  `SUPPORTED_CODES` language. App labels take a different path —
  `website/src/components/appstore/appManifest.ts::appPageLabel` looks the app id up
  in the hardcoded `APP_MANIFEST_KEY` table, which covers first-party apps only, and
  anything outside that table falls back to the raw `page.label`. So a third-party
  app's rail label is structurally exempt from the AGENTS.md rule ("never hardcode a
  user-facing English string") that the rest of the dashboard is CI-gated on, and
  there is no path for a third-party app to supply a translation at all.

None of this is a missing design. `ui.sidebar` **is** the design; it was shipped in
the manifest and never connected. This RFC finishes that contract first, and then
decides separately, on evidence, whether to widen it.

## 2. Design principles

1. **Finish the contract that exists before inventing a new one.** `ui.sidebar` is
   already public manifest surface with documented defaults. Honoring it is not an
   extension of the app API; discarding it is the bug.
2. **Core wins collisions.** Any new registration path routes through
   `website/src/apps/seamCollision.ts::reportSeamCollision`: core wins, throws in
   dev/test, warns in prod.
3. **Placement is a request, not a grant.** An app *asks* for a section; the host
   decides which sections are open to apps. An over-reaching request is clamped, not
   rejected — a manifest must never fail to install over rail cosmetics.
4. **A nav label is a user-facing string.** No seam may exempt one from the i18n
   rule. If a surface can name itself, it must be able to translate that name, and
   the test gate must cover it.
5. **Registries stay non-reactive.** A deliberate choice, not an omission (§6).

## 3. Architecture

### 3.1 Phase N1 — honor `ui.sidebar` and all pages

Replace the single-target resolver with a per-page one: `appNavTarget(app)` becomes a
per-page mapping returning one target per entry in `manifest.ui.pages`, each carrying
`section` and `order` resolved as

- `order`: `page.order ?? manifest.ui.sidebar.order` (default `10`);
- `section`: `manifest.ui.sidebar.section` (default `'Apps'`), passed through a
  host-side allowlist.

The allowlist is the whole authority model for this phase:

| Requested section | Outcome |
|---|---|
| `Apps` | granted |
| `Platform` | granted |
| `Main`, `Bottom` | **clamped to `Apps`**, with a dev-mode console warning naming the app |
| anything else | clamped to `Apps`, same warning |

`Main` and `Bottom` stay reserved for core and edition surfaces. Clamping rather than
erroring keeps a hopeful third-party manifest installable.

`sortedAppGroup` sorts by `(order, name)` — see §7.3 on why not `label`. The user's
dnd order remains an override layer on top: an id present in
`localStorage['mc-app-nav-order']` keeps its user position, and ids absent from it
fall back to declared `order`. Existing user state therefore needs no migration.

`isAppNavigable` keeps its current meaning (`enabled` and at least one page), so a
disabled app still contributes nothing.

Backend change: none. `UISidebar` already serializes and `GET /api/apps` already
returns the manifest.

### 3.2 Phase N2 — promote `registerBuiltinSurface` to the tenth seam

Add `order?: number` to `Surface` and expose `registerBuiltinSurface` through
`virtual:kirocrew-edition` alongside the existing nine, gated by the same
`KIROCREW_EDITION_DIR` + `KIROCREW_ALLOW_EDITION=1` pair, with collisions routed
through `reportSeamCollision`. Ordering within a group becomes `(order ?? index)`, so
today's registration-order behaviour is preserved when no `order` is given.

This is what makes `'Platform'` reachable and lets an edition place a surface in
`Main`/`Bottom`. It does **not** open `Main`/`Bottom` to apps: an edition is
first-party build-time code, an app is third-party runtime code, and §2 principle 3
keeps them on different sides of the allowlist.

N2 is gated on §7.1 — it should not ship without a named requester.

### 3.3 Phase N3 — a translation path for app-provided labels

1. `UIPage` gains an optional `labelKey: str = ""`, and an app may ship locale files
   at `ui/locales/<code>.json`.
2. `AppHost` merges those catalogs at mount under a reserved namespace,
   `apps.<name>.*`, so an app can overwrite neither a core key nor another app's.
3. `appPageLabel` resolution order becomes: `page.labelKey` (through the merged
   catalog) → `APP_MANIFEST_KEY` (retained for first-party apps that already have
   keys) → raw `page.label` → `displayName` → `name`.

`navLabels.test.tsx` is extended so that **any** surface carrying a `labelKey`, core
or app, must resolve non-empty in every `SUPPORTED_CODES` language. The raw-label
fallback stays legal — it is what an untranslated third-party app gets — but it stops
being the only option.

## 4. Migration plan

| Phase | Scope | Surfaces touched | Reversible |
|---|---|---|---|
| N1 | `appNav.ts`, `App.tsx` nav assembly | frontend only | yes, read-path only |
| N2 | `surfaces/registry.ts`, `extensions.ts`, the `vite.config.ts` seam list, `extension-seams.md` | frontend + docs | yes |
| N3 | `manifest.py` (`UIPage.labelKey`), `AppHost`, `appManifest.ts`, `navLabels.test.tsx` | backend field + frontend | yes, additive field |

N1 needs no data migration: an app that omits `ui.sidebar` gets today's behaviour
exactly (`section='Apps'`, `order=10`, and with a single page the result is identical
to `pages[0]`). N3's `labelKey` is additive and absent by default.

## 5. Security model

Nav placement grants no authority. A rail row does not widen `permissions.api`, and
`AppHost` continues to scope the app's API surface through `AppApiProvider` from
`manifest.permissions`. The app-scoped HMAC token path
(`token_auth._enforce_app_scope`) is untouched.

The one new risk this RFC introduces is **placement spoofing**: an app choosing a
section in order to read as a trusted core surface. Three controls:

1. `Main` and `Bottom` are refused to apps (§3.1) — the two pinned regions users read
   as "the product itself" stay closed.
2. App rows keep their existing visual treatment and route prefixes
   (`/apps/<name>` for AppHost-routed apps), so the destination stays legible.
3. `Apps` and `Platform` are sections users already read as third-party.

Nothing here relaxes admission: `app_admission.json`,
`governance_permits("apps", name)`, and `agent.apps_allow_third_party` all continue to
decide whether an app exists at all before any of this runs.

## 6. Non-goals

- **Reactive registries.** DeepSeek Harness makes registry mutation cascade through
  fiber dispose/remount. We deliberately do not adopt this. React's lazy routes
  already resolve a built-in route at navigation time, which is the property fiber
  reactivity would buy; a reactive registry only complicates the HMR boundary. The
  current registries are read at module load / first render and that is correct.
- **User-reorderable `Main`/`Bottom`.** The pinned regions stay pinned.
- **Module federation or iframe-hosted app UI.** The existing
  `import(/* @vite-ignore */ '/apps/<name>/ui/<entry>')` path is sufficient.
- **Letting an app replace a core surface.** Placement only; replacement is a
  different and much larger question (§8.1).

## 7. Open questions

1. **Does anything actually need `Main`?** N2 exists to let an edition place a
   surface in a pinned region. If no edition or first-party surface wants that today,
   N2 should stay unbuilt — building a seam with no requester is how the nine
   existing seams ended up inert in the stock build.
2. **Should `Platform` be user-hideable?** It is currently unrendered, so its
   interaction model is undefined.
3. **Tie-break on equal `order`.** Sorting by `label` would make the tie-break
   locale-dependent, which the i18n rule forbids doing casually; §3.1 therefore
   proposes the locale-free app `name` as the secondary key. Worth confirming that
   this reads sensibly to users.
4. **Interaction with `APPS_NAV_LIMIT = 6`.** An app that asks for `order: 1` and
   still lands behind the "N more" toggle is a surprising outcome; the overflow cut
   may need to respect declared order explicitly.

## 8. Alternatives considered

1. **Adopt DeepSeek Harness's plugin-kernel model** — every capability a named
   provider, configuration as assembly, the whole rail contributed by plugins.
   Rejected. Beyond being an architecture rewrite, DSH buys its composability with a
   property we cannot afford: no position can speak last. Its `reflect.provide`
   permits exactly one provider per service name per isolate realm, so substitution
   means deleting a row rather than layering a control; and its `approval/request`
   allow path is an ordinary prependable cordis waterfall — a plugin can answer
   `allowed-once` before any human UI sees the request, and DSH's own ACP package
   already registers a machine answerer on it. Its one order-proof gate is hardcoded
   to bypass its own event system. Kiro Crew's admission policy, governance ceiling,
   and per-app permission scoping have no expressible home in that model.
2. **Document `pages[0]`-only as intended and delete `UISidebar`.** Rejected:
   `UISidebar` is already published manifest surface with defaults and a documented
   meaning. Removing it breaks a contract we have already made, in exchange for
   nothing.
3. **Give apps a fully free rail** — any section, any order, no allowlist. Rejected:
   placement spoofing (§5) with no compensating control, for a convenience no app has
   asked for.
4. **Solve only the i18n hole (N3) and leave placement alone.** Viable but
   incomplete: it leaves the manifest lying about `section`/`order` and leaves
   multi-page apps unreachable. N3 is independently shippable, so this remains
   available as a reduced scope rather than a rejected alternative.
