# Frontend extension seams

Additive registries let a **downstream edition** (a separate build that composes
this SPA, for example an internal fork) contribute UI without copy-and-shadowing
core files. The core registers nothing new into them, so every seam is inert in
the stock build.

The backend has the sibling mechanism, Composed Platform Providers: see
[`docs/system-specs/modules/platform-context.md`](../../docs/system-specs/modules/platform-context.md).
The two are independent. Nothing here reads `CONTRACT_VERSION`.

## The fourteen registry seams

Each entry is one registrar the edition may call, paired with the reader the core
already calls. `src/extensions.ts` names exactly these fourteen in its header.
`src/test/extensionSeams.test.tsx` exercises each one except the source-provider
seam, which has its own suite in `src/test/sourceProviderSeam.test.ts`.

| Seam | Module | Registrar to reader |
|------|--------|---------------------|
| Builtin page routes | `apps/builtinRegistry.ts` | `registerBuiltinComponents()` to `getBuiltinComponent()` |
| Nav icons | `apps/builtinIcons.tsx` | `registerBuiltinIcons()` to `getBuiltinIcon()` |
| Theme branding | `themeBranding.tsx` | `registerThemeBranding()` to `getThemeBranding()` |
| Theme picker options | `hooks/useTheme.tsx` | `registerTheme()` to `getRegisteredThemes()` |
| Top-bar widgets | `apps/topBarWidgets.tsx` | `registerTopBarWidgets()` to `getTopBarWidgets()` |
| Readout-capsule segments | `apps/capsuleSegments.tsx` | `registerCapsuleSegment()` to `getCapsuleSegments()` |
| Overview status cards | `pages/overviewStatCards.tsx` | `registerOverviewStatCards()` to `getOverviewStatCards()` |
| Overview lower panel (single owner) | `pages/overviewPanel.tsx` | `registerOverviewPanel()` to `getOverviewPanel()` |
| Overview built-in suppression (subtractive) | `pages/overviewBuiltins.ts` | `suppressOverviewBuiltin()` to `isOverviewBuiltinSuppressed()` |
| Panel-navigation chords | `hooks/useKeyboardShortcuts.ts` | `registerPanelShortcut()`, read by the shortcut handler and `DEFAULT_SHORTCUTS` |
| Non-app route prefixes | `components/MigrationCheck.tsx` | `registerNonAppPrefix()`, read by `MigrationCheck` |
| Source providers (Changes panel + sidebar chips) | `utils/pullRequestLinks.ts` | `registerSourceProvider()` to `sourceProviderDescriptor()` |
| Phone-connection method renderers | `components/mobileConnectRenderers.tsx` | `registerMobileConnectRenderer()` to `getMobileConnectRenderers()` / `canRenderMobileConnectKind()` |
| Bare-token autolink rules | `utils/autolinkRules.ts` | `registerAutolinkRules()` to `getAutolinkRules()` |

Plus one **exported-transport** seam for edition-owned API methods. It is not a
registry; see "API methods" below.

Other `register*()` functions in `src/` (built-in surfaces, command-palette
providers, tool pills, terminal sockets, highlight.js languages) are core-internal
wiring, not edition seams. Only the fourteen above are called from the composition
root.

Thirteen of the fourteen are **additive** — the edition contributes a surface. The
remaining one is **subtractive**: `suppressOverviewBuiltin()` removes a built-in
Overview surface for a distribution whose environment makes it permanently
inapplicable, which no additive seam can express. It is named `suppress*` rather
than `register*` precisely so a call site cannot be misread as a contribution.

## Composition root

`src/extensions.ts` is **core-owned** and imported first in `main.tsx`, before the
store, the providers, and `App`, so all registration runs before render. Its whole
body is one side-effect import of the `virtual:kirocrew-edition` module plus
`export {}`. `extensionSeams.test.tsx` strips comments and asserts exactly that
body, so a core registration added here fails a test rather than quietly ending
the stock build's no-op property. Core registrations belong in the seed maps
(`BUILTIN_COMPONENT_REGISTRY`, `BUILTIN_ICON_REGISTRY`, `THEMES`, and so on).

`editionExtensionPlugin` in `vite.config.ts` resolves the virtual module to:

- an **inert empty module** in the stock OSS build (`KIROCREW_EDITION_DIR` unset),
  so the stock build registers nothing and is byte-identical to having no seam;
- the **edition's own** `$KIROCREW_EDITION_DIR/extensions.tsx` (or `.ts`) when that
  env var points at an edition repo, so the edition injects its `register*()`
  calls and component imports by build config, compiled through the same
  vite/rollup pass, without shadowing or overlaying any core file. That
  copy-and-shadow erosion is what the seams exist to eliminate.

Resolution is eager, so a misconfigured `KIROCREW_EDITION_DIR` (set but with no
`extensions.tsx`/`.ts` inside) **fails the build loudly** instead of silently
degrading to the stock SPA, which would ship an edition build with none of its
edition behavior.

## Edition-build safety: fail-closed opt-in

Edition composition needs **two** env vars, not one. `KIROCREW_EDITION_DIR` alone
throws: the plugin also requires `KIROCREW_ALLOW_EDITION=1`.

Why the opt-in exists in this direction: an edition build compiles that edition's
proprietary sources into `website/dist`, and that `dist` is staged into the public
OSS wheel. A published release cannot be unpublished, so contamination is a
one-way door. With the opt-in as the gate, every pipeline (release, publish, and
the backend `setup.py` to `build-frontend.sh` path) is protected **by default**: a
stray or inherited `KIROCREW_EDITION_DIR` fails the build instead of silently
compiling edition sources into a public artifact. Only the edition's own build
script sets the opt-in. Forgetting it fails safe (stock), and there is no guard
variable a release job must remember to set. Never set
`KIROCREW_ALLOW_EDITION=1` in a release or publish job.

An edition-mode build also prints a loud self-identifying warning naming the
resolved composition root, so the mode is unmissable in local and CI logs.

## The RUNTIME rebuild threads the seam too

`POST /api/update`, `kirocrew update`, and the gateway's auto-apply all shell
`npm run build` and stage the result over the served `static/dist`. Vite reads the
composition root from the environment, so what those rebuilds pass decides **which
edition gets built** — and both ways of getting it wrong are silent:

dropping the vars compiles the **stock** SPA over an edition dashboard — the build
succeeds, so nothing raises; the dashboard just becomes upstream's.

`frontend._edition_build_env()` forwards the pair, and **reads the opt-in rather
than synthesizing it**: forcing `KIROCREW_ALLOW_EDITION=1` would defeat the
fail-closed gate above precisely when it should fire, quietly turning an
edition dir left in a gateway's environment into edition-composed *packaged* data.
So an edition dir without the operator's own opt-in returns `None` and the
plugin's explicit error stands. With no edition dir it also returns `None`, so the
stock path inherits the environment unchanged.

A packaged install (wheel or bundle) ships the built `dist` but **not** the
edition's TypeScript sources, where a rebuild could only produce a stock bundle.
`frontend.edition_sources_missing()` detects that and the rebuild is **skipped**,
keeping the shipped dashboard. Covered by `test/test_frontend_edition_build.py`.

## Pre-boot shell branding: `branding.json` and the `public/` overlay

Everything the registry seams brand is rendered by React — which means none of it
exists before React mounts. Three surfaces are shown earlier: the `<title>` during
boot (and whenever a PWA-install dialog or bookmark samples it), the
`<meta name="theme-color">`, and the PWA identity (`manifest.json` plus
`icon-192.png` / `icon-512.png`). `registerThemeBranding()` cannot reach any of
them. The edition plugin patches them instead — the HTML fields on every build
and dev transform, the `public/` overlay at build emit — from two optional
inputs in the edition dir:

- **`branding.json`** — `{"title": "Acme Crew", "themeColor": "#0055aa"}`. Both
  keys optional; values are HTML-escaped into the root `index.html` only (app
  panel pages are untouched). An unknown key, a non-string value, or malformed
  JSON **fails the build** — a typoed key silently shipping the stock title is
  the exact silent-degrade class this seam bans. So does a missing target tag:
  if the core shell drops `<title>` or the theme-color meta, the edition build
  breaks loudly rather than quietly reverting to stock.
- **`public/`** — files here overlay the stock copies in the built `dist`,
  edition-wins, emitted through the bundler (`generateBundle`/`emitFile`, which
  takes precedence over the `publicDir` copy). Only an allowlist is accepted —
  `manifest.json`, `icon-192.png`, `icon-512.png` — and any other file or
  subdirectory in the edition's `public/` fails the build (OS junk dotfiles like
  `.DS_Store` are skipped). The allowlist is the structural guarantee
  that an edition cannot overwrite `index.html`, `sw.js`, or `vendor/*`; widen it
  consciously (`SHELL_OVERLAY_ALLOWLIST` in `scripts/lib/editionShell.mjs`).

Two known edges: the overlay is **build-only** (`generateBundle` never runs in
the dev server, which keeps serving the stock `public/` files — the branded
manifest/icons appear in `dist`), and `branding.json` is read eagerly at config
load, so the dev server needs a restart after editing it.

Both inputs are inert when absent, and the stock build (no `KIROCREW_EDITION_DIR`)
is byte-identical to a build without this seam.

**Replacing `icon-512.png` obliges you to replace the served logo too.** The
gateway serves `/logo.png` (sidebar logo, favicon fallback, chat avatar) from its
own static tree, and core CI pins that file byte-identical to
`website/public/icon-512.png` so the installed-app icon and the favicon can never
drift apart. An edition that overlays the PWA icons but leaves the gateway logo
stock reintroduces exactly that drift — brand one, brand both.

## Edition peer-dependency rule

An edition dir resolves bare imports from its OWN `node_modules`, so any
**context-carrying singleton** the core's provider tree owns must be de-duplicated
or the edition's hooks bind to a second instance. The symptoms are
`Invalid hook call` (React), `No QueryClient set`, a null router context, or
silently empty data, and they appear only at runtime, only in the edition build.

`resolve.dedupe` in `vite.config.ts` covers seven packages: `react`, `react-dom`,
`react-redux`, `react-router`, `react-router-dom`, `@tanstack/react-query`,
`framer-motion`. **When the core adds a new global-context provider, add its
package to that list**, and the edition should declare these as peer deps. The
dedupe is harmless in the stock single-`node_modules` build.

## Authoring an edition: the build pitfalls

The seams above make an edition build possible; this section is what makes a
first one work. Each pitfall below is silent or misleading at the moment it is
introduced, and every one was hit in practice by a real downstream edition.

### Theme CSS: never rely on load order, take specificity

An edition that registers a theme (`registerTheme()`) ships that theme's CSS
block in its own file, imported from its composition root. In the current
build that CSS lands in the entry chunk's stylesheet, which `index.html` links
**before** the chunk carrying the core's `index.css` — but chunk order is an
artifact of the build, not a contract. What is contractual is the cascade: the
core's default palette block sets the theme variables on
`:root, [data-theme="dark"], …`, and `:root` is specificity (0,1,0). An edition
block headed `[data-theme="acme"]` is also (0,1,0), so whichever stylesheet
loads later wins — today that is the core, and every variable its default block
also sets silently overrides the edition's. Nothing errors: the picker shows
the theme, the palette stays stock.

The built-in themes never hit this because their blocks live in `index.css`
itself, after the default block — same sheet, later, wins.

Prescription: prefix the edition's theme selectors with `html`, which wins on
specificity regardless of load order:

```css
/* loses: (0,1,0), and the core's :root default block loads later */
[data-theme='acme'] { --accent: #0055aa; }

/* wins: (0,1,1) beats (0,1,0) in either load order */
html[data-theme='acme'] { --accent: #0055aa; }
```

### Bare imports: the edition dir needs its own `node_modules`

The composition root lives outside the SPA root, and Node-style resolution
walks **up from the importing file** — it never reaches
`website/node_modules` from a sibling repo. The first bare specifier in the
edition (`import { Sparkles } from 'lucide-react'`) fails the build:

```
[vite]: Rolldown failed to resolve import "lucide-react" from
"<edition dir>/extensions.tsx".
```

Give the edition dir its own `node_modules`: either a real install that
declares the shared packages as peer dependencies, or a build-script symlink to
`website/node_modules`. Either way, read the "Edition peer-dependency rule"
above — once two `node_modules` trees exist, every context-carrying singleton
must stay deduplicated or hooks bind to a second React.

### Typecheck the edition, or ship ReferenceErrors

The core's `tsc -b` covers `website/src` only (`tsconfig.app.json` has
`"include": ["src"]`), so the edition's sources are outside every typecheck the
core runs. The bundler does not fill the gap: TypeScript is erased, and a free
identifier — a typo like `registerThemee` — compiles into the bundle as an
assumed **global**. The build succeeds, `tsc -b` stays green, and the app
throws `ReferenceError` at module load. Because the composition root runs
before `App` mounts, that is a blank page, not a broken widget.

Give the edition a `tsconfig.json` that extends the core's and run it in the
edition's own build or CI (`npx tsc -p <edition>/tsconfig.json`) — the core
will never run it for you:

```jsonc
{
  "extends": "../KiroCrew/website/tsconfig.app.json",
  "compilerOptions": {
    "noEmit": true,
    // Without vite/client, every `import.meta.env` the edition touches
    // (directly or via a core module it imports) is a TS2339 false positive.
    "types": ["vite/client"]
  },
  "include": ["."]
}
```

`extends` keeps the `@/*` path mapping working (TypeScript resolves inherited
`paths` relative to the config that declares them), so the edition's
`import { registerTheme } from '@/hooks/useTheme'` typechecks against the real
core sources. With this in place the typo above is caught at build time:
`TS2552: Cannot find name 'registerThemee'. Did you mean 'registerTheme'?`

### A default theme needs configuration, not a seam

To make the edition's theme the default, do not look for a frontend seam —
seed `dashboard.theme_color` (and `theme_mode`) in the deployment's
`config.json`. The dashboard applies the server value from
`GET /api/theme/boot` over any stored client choice and writes it back, so a
fresh install lands on the edition's theme and the user keeps free choice from
then on. One caveat: the very first paint, before that response arrives, uses
the compiled-in default (`DEFAULT_COLOR_THEME`); a returning visitor is
unaffected because the applied value persists in `localStorage`.

## Collision policy

`apps/seamCollision.ts` is the one policy every **additive** registrar routes
rejections through. A registration whose key collides with a core entry (or an
already-registered one) is resolved core-wins, and `reportSeamCollision`:

- **fails loud in dev and test** (it throws under `import.meta.env.DEV`, which is
  true under Vite dev and vitest), so a colliding upstream sync is caught at
  build/test time rather than by an end user;
- **degrades safe in production** (warn and ignore), so a shipped app never
  white-screens over a duplicate.

The subtractive seam is deliberately **exempt**. `suppressOverviewBuiltin()` is a
set, and a repeat is not a conflict: two owners cannot share one render slot, but
two parties that both want a surface gone agree. So re-entrant registration (HMR,
a module imported twice) is silently idempotent rather than a
`reportSeamCollision` — which is why it is the one seam whose second call is not
an error.

## Per-seam validation

**Builtin routes.** `registerBuiltinComponents()` accepts only a single, plain
top-level path segment, `/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/`. `BuiltinAppRoute`
resolves the catch-all `/:builtinApp` from one path parameter and matches only
`location.pathname`, never the query or hash. So a multi-segment (`/reports/daily`),
query (`/reports?daily`), hash (`/reports#x`), whitespace, or `.`/`..` route would
register but never resolve, and navigation would redirect to chat. The mandatory
alphanumeric first character is what excludes `.` and `..`. A non-conforming route
routes through `reportSeamCollision`.

**Panel shortcuts.** `registerPanelShortcut({ code, path, label })` identifies the
chord solely by `KeyboardEvent.code`, and the displayed key is derived from that
code, so the advertised chord can never diverge from the handled one. Beyond core
panel chords and prior registrations, it rejects any code in
`RESERVED_PANEL_CODES`: the Alt chords the handler consumes before panel routing
(shortcuts modal, settings, focus-input, MRU toggle, chat-jump digits, prev/next
arrows). A panel bound to one of those would be advertised but unreachable. The
label is used verbatim, because the core has no catalog key for a panel it does
not know about, so the edition owns its localization.

**Theme picker options.** `registerTheme([{ value, label }])` adds a built-in theme
to the picker; `useTheme` reads it via
`allThemes = [...THEMES, ...registered, ...customThemes]`. The theme's CSS block
ships in the edition's own overlay: this seam contributes only the picker entry. A
`value` already in `THEMES`, or already registered by an earlier call, is rejected
(core wins).

**Theme branding reaches three consumers.** `getThemeBranding(colorTheme)` drives
the `App.tsx` shell chrome, `WelcomeView.tsx` (the new-session brand mark), and
`pages/chat/ChatFooter.tsx` (the turn-running loader). A registered theme's `logo`
shows in the first two, falling back to the stock ghost mark when the theme
registers none. The loader contract is documented in
[theming-contract](theming-contract.md).

A branding's optional `onActivate` side-effect fires on each transition into that
theme, including the first render for the initially-active theme, because the
"previous theme" ref starts empty. Keep it idempotent and cheap. `App.tsx` wraps
the call in a `try`/`catch` so an edition-owned effect that throws cannot take
down the shell, but it still logs. `favicon` is handled the same generic way: the
core has no per-theme favicon and falls back to `/logo.png`.

**Readout-capsule segments.** `registerCapsuleSegment([{ id, order?, component, hideOnMobile? }])`
mounts a status segment INSIDE the header's readout capsule, sharing its border,
`|` dividers, and offline tint, rather than as a standalone sibling pill. Choose
this over `registerTopBarWidgets` when the readout must join that grouping (a
credential-TTL or spend segment, say). `App.tsx` splices registered segments after
the core segments in ascending `order`; each renders with an `offline` prop and is
isolated in its own `ErrorBoundary` with `fallback={null}`.

**Top-bar widgets.** `registerTopBarWidgets([{ id, component }])` mounts a
standalone pill in the header's right-hand actions area, next to the capsule.
Widgets render in insertion order, take no props (each reads its own state or
queries), and are each `ErrorBoundary`-isolated.

**Theme centre decoration is a backdrop, not a cell.** `branding.topBar` used to
render as a sized flow cell between the search and the actions group
(`flex-1 min-w-0 h-full`). Under the three-track grid it renders as a full-header
background layer instead: `absolute inset-0`, `pointer-events-none`,
`aria-hidden`. A fourth in-flow child would land in an implicit column and shift
the search off centre, and a sweep or scanline is visually a backdrop anyway. The
narrowed contract: a registered decoration **cannot receive pointer events** and
is **not announced**, so an interactive or gap-sized decoration degrades silently
(the `ErrorBoundary` never fires — nothing throws). Register interactive chrome
through `registerTopBarWidgets` instead.

**Rung thresholds are locale-measured.** The container-query breakpoints in
`.topbar`'s ladder (`src/index.css`) are the measured content width of each
readout tier plus a margin, taken through
`website/capture/topbar-search-variants.tsx`. The base rungs are measured in one
locale; the update-pill shift (below) is measured across every shipped locale. A
wider-than-measured tier squeezes or truncates its text before the rung fires —
graceful, but it means the constants are an approximation, not a guarantee.
Re-measure with that harness when readout content or the catalogs change
materially.


**Width budget for both top-bar seams.** The header is a three-track grid whose
side groups are pure remainder (`minmax(0,1fr)`, no floor) — see `.topbar` in
`src/index.css`. The actions group therefore does NOT grow to fit its contents;
it gets what the window leaves after the centred search, and its built-in
readouts give that space back through container-query rungs. Registered segments
and widgets do not participate in those rungs, so a registered component must
stay inside a budget: **keep the collapsed form under ~40px** and drop your own
labels with your own `@container` rule keyed off `.tb-right` if you render text.
The narrowest desktop width leaves the group about 206px, of which the built-in
dot, metric icon, credit icon and bell already claim roughly 139px. A component
wider than the remainder is clipped from the group's leading edge (the group
clips deliberately rather than pushing the notifications bell out of the
header), and at the terminal rung the capsule is reduced to its connection dot,
which hides registered segments along with the core readouts.

**The budget has TWO bases.** While an update is pending, the top bar mounts the
update pill — a non-shrinking sibling of the ladder — and the actions group
carries `tb-has-update`, which shifts the rungs by the pill's footprint (see the
rung comments in `src/index.css`). The footprint follows the pill's own label
gate (`hidden sm:inline`, 640px viewport): at ≥640px it is the widest
shipped-locale label form plus the group gap (201.7px + 6px = 208) and every
rung shifts, terminal included (408px instead of 200px); below 640px the pill
is icon-only (34px + 6px gap = 40) and only the terminal rung shifts (240px).
The ≥640 shift is a deliberate over-reservation for every narrower-label
locale — static CSS cannot key a rung on the active language, so an English
pill (~134px) gives up readouts ~68px earlier than its own width requires, in
exchange for no locale ever re-entering the squeeze band. For a registered
segment that means the ~40px collapsed-form budget above holds only in the
no-update state; with an update pending the same window width leaves up to
208px less, and at the narrowest desktop widths the remainder for registered
content is zero. Treat the update-pending state as one of the widths your own
`@container` rule must survive.

**Overview status cards.** `registerOverviewStatCards([{ id, order?, component }])`
adds a self-contained `StatCard` (owning its own query and state, like the core
`TunnelStatus`) to the Settings Overview grid, after the core cards, in ascending
`order`. Each receives a `delay` prop for the grid's stagger animation.

**Overview lower panel.** `registerOverviewPanel({ id, component })` claims the
region below the Usage/Memory summary grid, which the stock build leaves empty.
Unlike every other registry here this slot holds **at most one** entry: the first
registration owns the region, and a second is a collision (throws in dev/test,
warns and is ignored in production) rather than appending or replacing. That is
the point — the region has one owner who renders whatever internal layout it
wants and owns all of it, so there is no layout negotiation between parties who
cannot see each other. Reach for `registerOverviewStatCards` instead when the
contribution really is one more tile in the status grid; use this slot when the
content does not fit a 150px tile. The component receives no props and is wrapped
in an `ErrorBoundary`, so a throwing panel disables only itself.

**Overview built-in suppression.** `suppressOverviewBuiltin(id)` takes an id from
a **typed union**, not a free string. That is the validation: a misspelled
free-form id would suppress nothing and say nothing, and that symptom is
indistinguishable from the seam not working at all, so the union turns it into a
compile error at the call site. Keep the union minimal and add a member only
alongside a real consumer — an id with no caller is API surface that has never
been exercised. The seam is **one-way** (there is no `unsuppress`) and, like every
registry here, is read at render and not reactive, so suppression must be
registered during composition.

It is **not a security control**. Suppression removes a piece of guidance from one
page and relaxes nothing: whatever policy made the surface inapplicable is still
enforced server-side (for `tailnet-mobile` the status endpoint still derives its
step and the QR mint still refuses a pinned install with `governance_pinned`), so
hiding a card cannot grant access the backend would otherwise deny. At the render
site the gate sits outside both the `ErrorBoundary` and the spacing wrapper, so a
suppressed build emits no element at all rather than an empty, still-spaced one.

**Non-app route prefixes.** `registerNonAppPrefix(prefix)` tells `MigrationCheck`
that a route can never host a migratable app, so the migration banner does not
probe it. A duplicate prefix is a no-op.

**Source providers.** `registerSourceProvider(descriptor)` adds a code-review
forge to link extraction, sidebar chips, and the Changes panel. It is the one
seam whose registration is HALF a provider: the descriptor covers parsing and
rendering (`parse`, `chipLabel`, `refLabel`, an optional `icon` glyph, and the
`capabilities` flags gating each write affordance), while fetching and every
mutation are served by a backend plugin the edition registers with
`register_source_provider()` in
`src/kiro_crew/dashboard/handlers/source_providers.py`, under the same id. The
two registries validate the same id grammar (`/^[a-z][a-z0-9_-]{0,31}$/`) and
both refuse the built-in ids (`github`, `gitlab`, `jira`), so a descriptor can
never restyle a core provider and a payload provider id round-trips through both
layers. A descriptor missing `parse`/`chipLabel`/`refLabel`/`capabilities`
routes through `reportSeamCollision`.

Every capability flag names the backend hooks it commits the plugin to (see
`SourceProviderCapabilities` in `utils/pullRequestLinks.ts`): a flag set without
its hooks renders a button whose call can only fail, which is exactly what the
flags exist to prevent. Descriptor-returned links are re-validated
(`validRegisteredLink`): the link must carry the descriptor's own id, an
`https://` canonical URL that survives persist-and-reparse, and `kind: 'change'`
— issue refs are refused at admission because the issue pipeline is
built-in-only. A provider id the frontend has no descriptor for renders through
fail-closed fallback meta (`utils/sourceProviderMeta.ts`): neutral labels, no
logo, no write affordances. The backend plugin contract — payload schema
(`SourceChangePayload`), shared caches, redaction, byte caps, the optional
mutation hooks, and the optional DISCOVERY hooks `path_markers()` and
`search_ref()` — is documented on `SourceProviderPlugin` in
`source_providers.py`. The discovery hooks exist because a built-in-only
recogniser is blind to an edition's own id and URL shapes: `path_markers()`
contributes the URL substrings worth parsing, so an edition's chips appear at
all, and `search_ref()` contributes the spellings of one item, so a transcript
citing an edition's review by URL is found by its id. Both are optional, both are
bounded per plugin in core, and a plugin that raises is isolated per provider so
it cannot suppress another's — for `search_ref()` the search module's own guard
around the resolver contains it a second time, degrading the query to a literal
needle. This seam's suite
is `src/test/sourceProviderSeam.test.ts`
plus `test/test_source_provider_plugin.py` on the backend.

**Phone-connection method renderers.**
`registerMobileConnectRenderer({ kind, component })` supplies the "Connect your
phone" dialog section that draws one `MobileConnectMethod.kind` contributed by the
backend `mobile_connect` CPP seam. It keys on `kind`, not `id`, because that is
the descriptor's own split: `id` is the governed identifier the
`capabilities.mobile_connect` `methods` ruleset narrows on, while `kind` exists to
name the renderer — so two methods may share one kind and a component that needs
the ids reads `/api/mobile-connect/methods` itself. A blank kind, a duplicate, or
a **built-in** kind (`tailnet_qr`, `login_link`) routes through
`reportSeamCollision`: those two are drawn by core sections whose mint endpoints
the core audits, so registering over one would be an override that silently
redirects a credential mint, not a contribution.

The registry is also the **single** definition of the renderable set, read by two
consumers that used to carry it as matching literals: `canRenderMobileConnectKind()`
gates the nav rail's row and `getMobileConnectRenderers()` supplies the dialog's
sections. A kind neither drawn nor registered is still filtered out at the rail, so
the row stays hidden rather than opening a dialog with an empty body — the seam adds
a way to draw a method, it does not remove that guard. Registered sections render
above the built-ins, each in its own `ErrorBoundary`, so a throwing renderer
disables only itself. It **cannot widen governance**: the endpoint filters every id
through `capabilities.mobile_connect` before the dialog sees a kind, and each mint
endpoint re-runs that decision (`mint_denied_reason`), so a renderer for a denied or
unoffered method draws nothing.

**Bare-token autolink rules.**
`registerAutolinkRules([{ id, pattern, href }])` teaches the markdown renderer that
a bare token is an address. GFM already autolinks anything carrying a scheme; what
it cannot know is that in a given organisation `TICKET-1234` is a link. The core
registers none, so the stock build is byte-identical — the plugin returns before
walking when the registry is empty — and the vocabulary stays
downstream, where it belongs: a token scheme usually names infrastructure specific
to one deployment.

`getAutolinkRules()` is the reader, returning rules in **registration order**;
where two rules match overlapping spans the earlier-registered one wins.
`remarkAutolinkRules` is the consumer, ordered last in `REMARK_PLUGINS`.

Everything is validated at **registration**, so a bad rule fails once and loudly
instead of on one unlucky message: a sticky pattern is refused, an empty-matching
pattern is refused, a missing `g` is added, and `href` must be an absolute
`http(s)` URL template containing `{match}`, with no userinfo and the placeholder
outside the authority. `{match}` is substituted percent-encoded, which is what
makes that single check sufficient — a token cannot introduce a scheme, userinfo,
host or separator, so no per-match re-check could reach a different verdict.

The safety argument for *where* rules are applied belongs to the plugin and is
documented in `website/src/utils/remarkAutolinkRules.ts`.

## Reactivity

Registries are read at module load or first render and are **not reactive**. The
edition registers through the `extensions.ts` import path, before `main.tsx`
mounts `App`; registering after mount does not appear until an unrelated
re-render. Builtin routes are the one relaxed case, because they resolve lazily on
navigation.

## Product name: exported setter, not a registry

The i18n catalogs interpolate `{{productName}}` instead of hardcoding the
displayed product name (authoring rules:
[i18n-catalog](i18n-catalog.md#the-product-name-is-an-interpolation-variable)).
`initI18n()` feeds the variable to i18next as `interpolation.defaultVariables`,
defaulting to `Kiro Crew`, so the stock build renders unchanged text.

An edition rebrands by calling `setProductName('…')` (exported from
`src/i18n`) in its composition root. The root is imported before `main.tsx`
calls `initI18n()`, so the ordering holds by construction; a call after init
throws in dev rather than half-applying (in production it returns silently
rather than crash the shell). Like the API transport above, this is
a single exported function rather than a registry: the core consumes the value
itself, there is nothing to enumerate, and a whole-catalog transform hook would
hand an edition the power to break any string for what is a one-variable
substitution.

Scope: catalog strings only. The `apps.<id>.manifest.*` keys mirror the
Python-side `app.json` prose byte-for-byte and keep the literal name; the
shell logo and welcome mark are the theme-branding seam's job; the chat bot
display name stays `dashboard.bot_name`.

## API methods: exported transport, not a registry

There is no registrar for edition API methods. The core never *consumes* them:
they are written and read only by the edition. A registry the core never reads
would add public, stringly-typed (`unknown`-cast) seam surface for zero
composition benefit.

So `api/apiTransport.ts` **exports** the blessed `apiTransport`, the same
`get`/`post`/`put`/`del`/`patch` plus `j`/`jNullable` the core methods use
(`client.ts` installs them via `installApiTransport` at its module load). An
edition builds its OWN fully-typed API module on it:

```ts
import { apiTransport as t } from '../api/apiTransport'
export const editionApi = {
  sessionTtl: () => t.get('/api/session-ttl').then(t.j) as Promise<SessionTtl>,
}
```

That gives the edition the two things it needs by construction: the
`X-Session-Key` header and the auth-recovery / `ApiError` pipeline, with full
static types on the edition side and no new *registry* contract. It never forks
`client.ts` and never writes raw `fetch`, which would silently drop the session
key.

`ApiTransport` (the five request helpers plus the `j`/`jNullable` semantics) **is**
a small, intentionally frozen downstream contract, because a separately built
edition compiles against it. There is no version guard on this seam, and the stock
build stays green whatever you do to it (the seam is inert), so breakage surfaces
only at runtime in the out-of-repo edition. Changing a request helper's shape or
`j`'s error behavior is edition-breaking, not a free refactor. Evolve additively.

Each `apiTransport` method is a stable wrapper that resolves the installed helper
at call time, so an edition may import and even destructure it at module init
without an ordering hazard against `extensions.ts`.

Trust boundary: the transport carries the session key. It is for the edition
composition root, **never** for app or plugin-contributed frontend code.
