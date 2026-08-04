# Frontend conventions

Shared components, accessibility, security, data fetching, animation, styling,
typography, and how a builtin app gets discovered. Page structure is in
[page-layout](page-layout.md); color and CSS-var rules are in
[theming-contract](theming-contract.md); user-facing strings are in
[i18n-catalog](i18n-catalog.md).

## Shared components

`src/components/ui.tsx` is the primitive set. Compose from it rather than
hand-rolling:

`Card`, `CardTitle`, `Btn`, `SendBtn`, `IconButton`, `IconButtonGroup`, `Input`,
`SearchInput`, `Badge`, `SourceBadge`, `StatCard`, `Skeleton`,
`ContentSkeleton`, `SkeletonToggleRow`, `SkeletonField`, `SkeletonInfoRow`,
`FormSkeleton`, `EmptyState`, `PanelSectionHeader`, `PageHeader`, `Toggle`,
`Slider`, `Checkbox`, `Select`.

The provenance pill is **`SourceBadge`**, not a badge named after any one source.
Two implementations exist on purpose:
`ui.tsx`'s takes a required `source` string and renders it as the label;
`components/SourceBadge.tsx`'s takes an optional `source` plus `children`, so a
caller can render highlighted or translated label content over the same color
mapping. Both fall back to a neutral pill for an unrecognized source, so a new
source value degrades rather than throwing.

`PanelSectionHeader` is the one idiom for a counted list-section header inside a
side panel (label, count node, hairline rule). Route a new panel section through
it. The Files and Artifacts tabs each grew their own and silently diverged on
case, size, color, and whether the count was a node or punctuation baked into the
translated label.

Other shared modules:

- `Clickable.tsx` (accessible clickable div; see below)
- `SegmentedControl.tsx` (sliding tab selector, Framer Motion)
- `DetailPanel.tsx` (resizable side panel with animated open/close)
- `SidePanelLayout.tsx` (shared side-panel page layout)
- `AgentSelector.tsx` (portal dropdown with ARIA)
- `layout.ts` (`LAYOUT` numeric constants: nav widths, sidebar width, max message
  width, topbar height, log line cap)
- `InfoTip.tsx`, `MarkdownRenderer.tsx` (highlight.js syntax highlighting),
  `TypewriterText.tsx`

`src/kirocrew-ui/index.ts` re-exports the subset that apps may import as
`@kirocrew/ui`. Adding a primitive there makes it app-facing API, so add
deliberately.

## Accessibility

Every interactive element MUST be keyboard accessible. Use `Clickable` from
`src/components/Clickable.tsx` instead of `<div onClick>`; it applies
`role="button"`, `tabIndex`, Enter/Space handling and `aria-disabled` together, so
the three can never drift apart.

```tsx
// Good
import Clickable from '../components/Clickable'
<Clickable onClick={handler} className="...">Click me</Clickable>

// Bad: not keyboard accessible, fails jsx-a11y lint
<div onClick={handler} className="...">Click me</div>
```

`Clickable` self-activates only on keydowns whose `target` is the element itself,
never ones bubbling up from a focusable descendant. Without that guard a container
would hijack a nested control's native activation, and its `preventDefault()`
would swallow spaces typed into a nested input.

For an animated interactive element, wrap `Clickable` with Framer Motion. It
forwards refs and spreads props, so animation and a11y compose:

```tsx
import { motion } from 'framer-motion'
import Clickable from '../components/Clickable'
const MotionClickable = motion.create(Clickable)
```

Rules:

- Never `<div onClick>` or `<span onClick>` without `role="button"` + `tabIndex` +
  `onKeyDown`. Prefer `Clickable`, which handles all three.
- Every icon-only button needs an `aria-label` describing the action.
- Modals need `role="dialog"`, `aria-modal="true"`, an `aria-label`, Escape
  dismissal, and a focus trap.
- Dynamic content that updates in place (streaming messages, notifications) uses
  `aria-live="polite"`.
- Do not use a raw `<button>`. Use `Btn` / `SendBtn` / `IconButton` (which carry
  the styling), or `Clickable` for a div-based control.

Tooling: `eslint-plugin-jsx-a11y` reports violations at lint time, and
`@axe-core/react` scans the live DOM in dev mode (findings land in the browser
console). Neither replaces a keyboard pass over a new control.

## Security: sanitize every HTML sink

All `dangerouslySetInnerHTML` content goes through DOMPurify, via
`src/api/helpers.ts`:

- `md(text)` renders markdown-like formatting and sanitizes the result.
- `sanitize(html)` is the DOMPurify wrapper for already-built HTML.
- `esc(text)` escapes plain text (use this when you do not need markup at all).

A bypass is an XSS bug, so there is no "just this once" case.

## URL sanitization

`react-markdown` strips protocols it does not know. `src/utils/urlTransform.ts`
re-allows the editor deep links, `vscode:` and `vscode-insiders:`, and delegates
everything else to `defaultUrlTransform`. It also requires the URL to carry more
than the bare scheme, so `vscode://` alone is not treated as a link.

Add a new protocol to `ALLOWED_PROTOCOLS` in that file, and only there. Each
addition widens what a model-authored or user-pasted link can launch on the host,
so treat it as a security change, not a formatting one.

## Data fetching

Always React Query (`useQuery` / `useMutation`) for server state. Do NOT use
manual `useState` + `useEffect` + `useCallback` for an API call. Prefer optimistic
updates through `queryClient.setQueryData`.

Query keys are arrays whose first element names the resource, kebab-case:
`['mcp-servers']`, `['agents-installed']`, `['agent-detail', name]`. Append the
parameters a fetch varies on, so a stale entry cannot serve a different subject.

Real-time updates arrive on a single WebSocket at `/api/ws`, read through
`useWebSocket`, which reconnects with capped exponential backoff (1s doubling to a
10s ceiling) and re-fetches state through Redux on reconnect instead of reloading
the page.

Redux Toolkit (`src/store/index.ts`) holds the cross-page shell state in **four**
slices:

| Slice | Owns |
|---|---|
| `dashboard` | SSE/WS connection state, chat slots, approval mode, optimistic slot add/remove, thunks for slot fetch and approval-mode change |
| `chat` | active slot, messages, session history with pagination, WS chunk/done handling, thunks for slot CRUD and history fetch/resume/delete |
| `notifications` | notification list with add/delete/clear plus their thunks |
| `instances` | the known Kiro Crew instances a user can switch between |

Server data belongs in React Query, not in a slice. Reach for Redux only when the
state is shell-wide and not a cached server read.

## Animations

- Framer Motion for orchestrated component transitions: enter/exit, layout
  animations, gesture-driven motion.
- Tailwind `transition-*` for simple state changes (hover, toggle, color).
- Tailwind `animate-*` for simple indicators (spin, pulse) and the shared
  `animate-rise` / `animate-scale-in` entrances.
- Do NOT add a new CSS `@keyframes`. The existing ones in `index.css` back
  specific low-level effects (skeleton pulse, caret blink, indeterminate
  progress); a new component animation goes through Framer Motion.

## Styling

Tailwind CSS with the custom theme in `tailwind.config.js`, and
`darkMode: ['selector', '[data-theme="dark"]']`, so dark mode is driven by the
`data-theme` attribute rather than the OS media query alone.

Colors come from CSS custom properties defined in `src/index.css`, including the
semantic roles `--aim`, `--clarify`, and the `--diff-*` family. Never a hardcoded
`#hex` / `rgb()` / `rgba()` literal; see
[theming-contract](theming-contract.md) for the variable set, the stable class
hooks, and the checker.

Built-in themes are picked in Settings, Display tab, and the choice syncs across
instances. Each theme has a dark and a light block, and the default theme's
`data-theme` is the bare `dark` / `light` rather than a prefixed slug.

Shared CSS utilities in `index.css`: `.top-bar-pill`, `.topbar-glass`,
`.scroll-shadow`, `.table-striped`, `.skeleton`, `.focus-ring`. A theme change
crossfades through a `transition` on `body`.

## Typography scale

Body is 14px (`0.875rem`, set on `body`). Descriptions and details use
`text-sm` (14px); labels, buttons and sidebar entries use `text-[13px]`; badges
and captions use `text-[12px]`; decorative icons `text-[10px]` to `text-[11px]`.
Code blocks are 13px mono.

Minimum readable text is 11px, and **nothing goes below 10px**. Do not use
`text-xs` (use `text-[13px]`), and do not use `text-[9px]` or smaller.

## Builtin app auto-discovery

A builtin app does not need a `NAV_ITEMS` entry, and `App.tsx` does not need a
route for it. `BuiltinAppRoute` resolves the catch-all `/:builtinApp` against the
registry in `src/apps/builtinRegistry.ts`.

To add one:

1. Create the page component under `src/apps/<name>/` (or `src/pages/`).
2. Export it as the module default.
3. Add one lazy entry to `BUILTIN_COMPONENT_REGISTRY`:
   `'/my-app': lazy(() => import('./my-app/MyAppPage'))`.
4. Declare `ui.pages` in the app's `app.json` manifest, and its `ui.icon` name.
5. If the icon is not already in `src/apps/builtinIcons.tsx`, add it to
   `BUILTIN_ICON_REGISTRY` (Lucide element, `size={16}`).

Components are lazy so a builtin app does not weigh on the initial bundle. The
route must be a single plain top-level path segment: the registry is matched
against `location.pathname` only, so a multi-segment, query, or hash route would
register and then never resolve. The same constraint and the reasoning behind it
are in [extension-seams](extension-seams.md), which covers registering routes and
icons from a downstream edition instead of editing the seed maps.
