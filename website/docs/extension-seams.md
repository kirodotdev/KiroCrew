# Frontend extension seams

Additive registries let a **downstream edition** (a separate build that composes
this SPA, for example an internal fork) contribute UI without copy-and-shadowing
core files. The core registers nothing new into them, so every seam is inert in
the stock build.

The backend has the sibling mechanism, Composed Platform Providers: see
[`docs/system-specs/modules/platform-context.md`](../../docs/system-specs/modules/platform-context.md).
The two are independent. Nothing here reads `CONTRACT_VERSION`.

## The nine registry seams

Each entry is one registrar the edition may call, paired with the reader the core
already calls. `src/extensions.ts` names exactly these nine in its header, and
`src/test/extensionSeams.test.tsx` exercises each one.

| Seam | Module | Registrar to reader |
|------|--------|---------------------|
| Builtin page routes | `apps/builtinRegistry.ts` | `registerBuiltinComponents()` to `getBuiltinComponent()` |
| Nav icons | `apps/builtinIcons.tsx` | `registerBuiltinIcons()` to `getBuiltinIcon()` |
| Theme branding | `themeBranding.tsx` | `registerThemeBranding()` to `getThemeBranding()` |
| Theme picker options | `hooks/useTheme.tsx` | `registerTheme()` to `getRegisteredThemes()` |
| Top-bar widgets | `apps/topBarWidgets.tsx` | `registerTopBarWidgets()` to `getTopBarWidgets()` |
| Readout-capsule segments | `apps/capsuleSegments.tsx` | `registerCapsuleSegment()` to `getCapsuleSegments()` |
| Overview status cards | `pages/overviewStatCards.tsx` | `registerOverviewStatCards()` to `getOverviewStatCards()` |
| Panel-navigation chords | `hooks/useKeyboardShortcuts.ts` | `registerPanelShortcut()`, read by the shortcut handler and `DEFAULT_SHORTCUTS` |
| Non-app route prefixes | `components/MigrationCheck.tsx` | `registerNonAppPrefix()`, read by `MigrationCheck` |

Plus one **exported-transport** seam for edition-owned API methods. It is not a
registry; see "API methods" below.

Other `register*()` functions in `src/` (built-in surfaces, command-palette
providers, tool pills, terminal sockets, highlight.js languages) are core-internal
wiring, not edition seams. Only the nine above are called from the composition
root.

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

## Collision policy

`apps/seamCollision.ts` is the one policy every registrar routes rejections
through. A registration whose key collides with a core entry (or an
already-registered one) is resolved core-wins, and `reportSeamCollision`:

- **fails loud in dev and test** (it throws under `import.meta.env.DEV`, which is
  true under Vite dev and vitest), so a colliding upstream sync is caught at
  build/test time rather than by an end user;
- **degrades safe in production** (warn and ignore), so a shipped app never
  white-screens over a duplicate.

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

**Overview status cards.** `registerOverviewStatCards([{ id, order?, component }])`
adds a self-contained `StatCard` (owning its own query and state, like the core
`TunnelStatus`) to the Settings Overview grid, after the core cards, in ascending
`order`. Each receives a `delay` prop for the grid's stagger animation.

**Non-app route prefixes.** `registerNonAppPrefix(prefix)` tells `MigrationCheck`
that a route can never host a migratable app, so the migration banner does not
probe it. A duplicate prefix is a no-op.

## Reactivity

Registries are read at module load or first render and are **not reactive**. The
edition registers through the `extensions.ts` import path, before `main.tsx`
mounts `App`; registering after mount does not appear until an unrelated
re-render. Builtin routes are the one relaxed case, because they resolve lazily on
navigation.

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
